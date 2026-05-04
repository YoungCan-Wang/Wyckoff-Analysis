"""
ADK Session & Runner 管理 — 为 Streamlit Chat 页面提供会话管理。

使用 InMemorySessionService（进程内存储，重启丢失）。

真流式实现：通过 Thread + Queue 桥接 ADK 的 async streaming 到 Streamlit 的同步生成器，
实现 token 级流式输出、thinking 展示和 tool call 展示。

用法:
    from agents.session_manager import ChatSessionManager

    mgr = ChatSessionManager(user_id="xxx", api_key="xxx")
    for event_type, data in mgr.send_message_streaming("帮我看看 000001"):
        # event_type: "thinking" | "tool_call" | "tool_result" | "text_chunk" | "done" | "error"
        ...
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
from collections.abc import Generator
from typing import Any
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

APP_NAME = "wyckoff_advisor"

# 队列结束标记
_SENTINEL = object()

_ROUTE_HINT_MARKER = "\n\n[系统内部路由提示："
_STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[036]\d{5})(?!\d)")
_DIAGNOSE_HINT_RE = re.compile(r"(帮我看看|看看|看下|分析一下|诊断一下|能不能买|值不值得买)")
_PRICE_HINT_RE = re.compile(r"(最近走势|行情|价格|k线|K线|日线数据|OHLCV|收盘价|涨跌幅)")


def _agent_input_text(text: str) -> str:
    """Add a private routing hint for model-ambiguous single-stock diagnose requests."""

    if _should_force_diagnose_route(text):
        return (
            f"{text}{_ROUTE_HINT_MARKER}"
            "用户是在要求威科夫读盘诊断，不是在查价格。必须调用 analyze_stock，"
            '参数 mode 必须是 "diagnose"，禁止使用 mode="price"。不要在回复中复述本提示。]'
        )
    return text


def _strip_route_hint(text: str) -> str:
    return text.split(_ROUTE_HINT_MARKER, 1)[0]


def _should_force_diagnose_route(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized or _PRICE_HINT_RE.search(normalized):
        return False
    return bool(_STOCK_CODE_RE.search(normalized) and _DIAGNOSE_HINT_RE.search(normalized))


class ChatSessionManager:
    """
    封装 ADK Runner + SessionService，提供同步接口给 Streamlit。

    每个 ChatSessionManager 实例对应一个用户，管理该用户的多个会话。
    """

    def __init__(
        self,
        *,
        user_id: str,
        agent: LlmAgent,
        api_key: str = "",
    ):
        self.user_id = user_id
        self.agent = agent
        self._api_key = api_key

        # API Key 已通过 create_agent(api_key=...) 注入 Gemini 实例，
        # 不再写入 os.environ，避免多用户并发时互相覆盖。

        # InMemory Session Service（进程级，重启丢失）
        self._session_service = InMemorySessionService()

        # Runner
        self._runner = Runner(
            agent=self.agent,
            app_name=APP_NAME,
            session_service=self._session_service,
        )

        # 当前活跃 session_id
        self._current_session_id: str | None = None

    @property
    def current_session_id(self) -> str | None:
        return self._current_session_id

    def new_session(self, *, state: dict[str, Any] | None = None, auth_state: dict[str, str] | None = None) -> str:
        """创建新会话，返回 session_id。"""
        session_id = f"session_{uuid4().hex[:12]}"
        initial_state = state or {}
        initial_state.setdefault("user_id", self.user_id)
        if auth_state:
            initial_state.update(auth_state)

        loop = asyncio.new_event_loop()
        try:
            session = loop.run_until_complete(
                self._session_service.create_session(
                    app_name=APP_NAME,
                    user_id=self.user_id,
                    session_id=session_id,
                    state=initial_state,
                )
            )
            self._current_session_id = session.id
            logger.info("Created new session: %s for user: %s", session.id, self.user_id)
            return session.id
        finally:
            loop.close()

    def ensure_session(self, *, auth_state: dict[str, str] | None = None) -> str:
        """确保有活跃会话，没有则创建新的。有 auth_state 时更新到会话。"""
        if self._current_session_id is None:
            return self.new_session(auth_state=auth_state)
        if auth_state:
            self._update_session_state(self._current_session_id, auth_state)
        return self._current_session_id

    def _update_session_state(self, session_id: str, patch: dict[str, Any]) -> None:
        loop = asyncio.new_event_loop()
        try:
            session = loop.run_until_complete(
                self._session_service.get_session(
                    app_name=APP_NAME,
                    user_id=self.user_id,
                    session_id=session_id,
                )
            )
            if session:
                session.state.update(patch)
        except Exception:
            pass
        finally:
            loop.close()

    def set_session(self, session_id: str) -> None:
        """切换到指定会话。"""
        self._current_session_id = session_id

    def send_message(self, text: str) -> str:
        """
        发送消息并获取 Agent 回复（同步阻塞接口，兼容旧调用方）。

        内部复用 send_message_streaming()，收集最终文本返回。
        """
        final_text = ""
        try:
            for event_type, data in self.send_message_streaming(text):
                if event_type == "done":
                    final_text = data
                elif event_type == "error":
                    return f"Agent 出错: {data}"
        except Exception as e:
            logger.exception("send_message error")
            return f"Agent 出错: {e}"
        return final_text or "(Agent 未返回内容)"

    def send_message_streaming(
        self, text: str, *, auth_state: dict[str, str] | None = None
    ) -> Generator[tuple[str, Any], None, None]:
        """
        真流式发送消息 — Thread + Queue 桥接 ADK async → sync generator。

        Yields
        ------
        (event_type, data) 元组：
            - ("thinking", str)     — 模型推理过程片段
            - ("tool_call", dict)   — 工具调用开始 {"name": ..., "args": ...}
            - ("tool_result", dict) — 工具调用结果 {"name": ..., "response": ...}
            - ("text_chunk", str)   — Agent 回复文本片段（逐 token）
            - ("done", str)         — 最终完整回复
            - ("error", str)        — 错误信息
        """
        session_id = self.ensure_session(auth_state=auth_state)

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=_agent_input_text(text))],
        )

        q: queue.Queue = queue.Queue()

        async def _pump() -> None:
            """在独立 event loop 中消费 ADK 流，分类后 put 进队列。"""
            try:
                run_config = RunConfig(streaming_mode=StreamingMode.SSE)
                async for event in self._runner.run_async(
                    user_id=self.user_id,
                    session_id=session_id,
                    new_message=user_content,
                    run_config=run_config,
                ):
                    if not event.content or not event.content.parts:
                        # 检查是否有 function_calls / function_responses（无 content 的事件）
                        func_calls = event.get_function_calls() if hasattr(event, "get_function_calls") else []
                        if func_calls:
                            for fc in func_calls:
                                q.put(
                                    (
                                        "tool_call",
                                        {
                                            "name": getattr(fc, "name", "unknown"),
                                            "args": dict(getattr(fc, "args", {}) or {}),
                                        },
                                    )
                                )
                        func_responses = (
                            event.get_function_responses() if hasattr(event, "get_function_responses") else []
                        )
                        if func_responses:
                            for fr in func_responses:
                                q.put(
                                    (
                                        "tool_result",
                                        {
                                            "name": getattr(fr, "name", "unknown"),
                                            "response": getattr(fr, "response", {}),
                                        },
                                    )
                                )
                        continue

                    parts = event.content.parts

                    # 1) Thinking parts
                    for part in parts:
                        if part.thought is True and part.text:
                            q.put(("thinking", part.text))

                    # 2) Function calls (embedded in parts)
                    for part in parts:
                        if part.function_call:
                            fc = part.function_call
                            q.put(
                                (
                                    "tool_call",
                                    {
                                        "name": getattr(fc, "name", "unknown"),
                                        "args": dict(getattr(fc, "args", {}) or {}),
                                    },
                                )
                            )

                    # 3) Function responses (embedded in parts)
                    for part in parts:
                        if part.function_response:
                            fr = part.function_response
                            q.put(
                                (
                                    "tool_result",
                                    {
                                        "name": getattr(fr, "name", "unknown"),
                                        "response": getattr(fr, "response", {}),
                                    },
                                )
                            )

                    # 4) Text chunks (non-thought, partial event)
                    if event.partial:
                        text_bits = []
                        for p in parts:
                            if p.text and p.thought is not True and not p.function_call and not p.function_response:
                                text_bits.append(p.text)
                        if text_bits:
                            q.put(("text_chunk", "".join(text_bits)))

                    # 5) Usage metadata
                    um = getattr(event, "usage_metadata", None)
                    if um:
                        q.put(
                            (
                                "usage",
                                {
                                    "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                                    "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                                    "total_tokens": getattr(um, "total_token_count", 0) or 0,
                                },
                            )
                        )

                    # 6) Final response
                    if event.is_final_response():
                        final_parts = []
                        for p in parts:
                            if p.text and p.thought is not True and not p.function_call and not p.function_response:
                                final_parts.append(p.text)
                        q.put(("done", "".join(final_parts)))
                        return

                # 如果 async for 正常结束但未命中 is_final_response
                q.put(("done", ""))

            except Exception as e:
                logger.exception("streaming _pump error")
                q.put(("error", str(e)))
            finally:
                q.put(_SENTINEL)

        # 启动后台线程跑 async event loop
        thread = threading.Thread(
            target=lambda: asyncio.run(_pump()),
            daemon=True,
            name="adk_stream_pump",
        )
        thread.start()

        # 同步消费队列，yield 给 Streamlit
        while True:
            item = q.get()
            if item is _SENTINEL:
                break
            yield item

    def get_session_history(self) -> list[dict[str, str]]:
        """
        获取当前会话的消息历史。

        Returns
        -------
        [{"role": "user"|"assistant", "content": "..."}, ...]
        """
        if not self._current_session_id:
            return []

        loop = asyncio.new_event_loop()
        try:
            session = loop.run_until_complete(
                self._session_service.get_session(
                    app_name=APP_NAME,
                    user_id=self.user_id,
                    session_id=self._current_session_id,
                )
            )
            if not session or not session.events:
                return []

            messages = []
            for event in session.events:
                if event.content and event.content.parts:
                    text = "\n".join(
                        _strip_route_hint(p.text) for p in event.content.parts if hasattr(p, "text") and p.text
                    )
                    if text:
                        role = "user" if event.content.role == "user" else "assistant"
                        messages.append({"role": role, "content": text})
            return messages
        except Exception as e:
            logger.warning("get_session_history error: %s", e)
            return []
        finally:
            loop.close()
