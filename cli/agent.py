# -*- coding: utf-8 -*-
"""
Headless agent loop — 无 UI 依赖的 agent 循环。

TUI 使用内联版本（与 Textual 渲染深度耦合），此模块为测试和非交互场景
提供可独立运行的 agent loop，支持 Rich Live 流式渲染。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from cli.compaction import compact_messages
from cli.loop_guard import (
    MAX_INCOMPLETE_TOOL_RETRIES,
    MAX_TOOL_ROUNDS,
    build_retry_exhausted_warning,
    build_retry_user_message,
    check_doom_loop,
    missing_required_tool,
    resolve_turn_expectation,
)
from cli.providers.base import LLMProvider
from cli.tools import ToolRegistry

logger = logging.getLogger(__name__)

_THINKING_TEXT = Text.from_markup("  [dim]思考中…[/dim]")


def run(
    provider: LLMProvider,
    tools: ToolRegistry,
    messages: list[dict[str, Any]],
    system_prompt: str = "",
    on_tool_call: callable = None,
    on_tool_result: callable = None,
    console=None,
) -> dict[str, Any]:
    """
    执行一次完整的 Agent 循环，流式渲染文本。

    Returns
    -------
    {"text": str, "usage": {"input_tokens": int, "output_tokens": int}, "elapsed": float}
    """
    total_input = 0
    total_output = 0
    t_start = time.monotonic()
    expectation = resolve_turn_expectation(messages)
    incomplete_tool_retries = 0
    used_tools_this_turn: list[tuple[str, dict]] = []
    _recent_calls: list[tuple[str, int]] = []

    _model_name = getattr(provider, "name", "")

    for round_idx in range(MAX_TOOL_ROUNDS):
        messages, _ = compact_messages(messages, provider, _model_name)

        text_buf = ""
        thinking_buf = ""
        tool_calls = None
        round_usage = {}
        live = None
        streamed = False  # 是否已流式渲染到终端
        in_thinking = False  # 是否正在展示推理过程

        try:
            # 启动 spinner（思考中）
            if console:
                live = Live(
                    Spinner("dots", text=_THINKING_TEXT),
                    console=console,
                    refresh_per_second=12,
                    transient=True,
                )
                live.start()

            first_token = True

            for chunk in provider.chat_stream(messages, tools.schemas(), system_prompt):
                if chunk["type"] == "thinking_delta":
                    thinking_buf += chunk["text"]
                    if live and not in_thinking:
                        # 首个 thinking token：spinner → dim 滚动文本
                        live.stop()
                        live = Live(
                            Text(thinking_buf[-300:], style="dim italic"),
                            console=console,
                            refresh_per_second=8,
                            transient=True,
                        )
                        live.start()
                        in_thinking = True
                    elif live and in_thinking:
                        # 只显示尾部，避免刷屏
                        live.update(Text(thinking_buf[-300:], style="dim italic"))

                elif chunk["type"] == "text_delta":
                    if first_token and live:
                        # 首个正文 token：结束 thinking/spinner → 流式正文
                        live.stop()
                        if in_thinking and console:
                            # thinking 结束，打印耗时
                            console.print(
                                f"  [dim]💭 推理完成 ({len(thinking_buf)} 字)[/dim]"
                            )
                        live = Live(
                            Markdown(chunk["text"]),
                            console=console,
                            refresh_per_second=8,
                            vertical_overflow="visible",
                        )
                        live.start()
                        first_token = False
                        in_thinking = False
                        streamed = True
                    text_buf += chunk["text"]
                    if live and not first_token:
                        live.update(Markdown(text_buf))

                elif chunk["type"] == "tool_calls":
                    if in_thinking and live:
                        live.stop()
                        if console:
                            console.print(
                                f"  [dim]💭 推理完成 ({len(thinking_buf)} 字)[/dim]"
                            )
                        live = None
                        in_thinking = False
                    tool_calls = chunk["tool_calls"]
                    partial = chunk.get("text", "")
                    if partial and not text_buf:
                        text_buf = partial

                elif chunk["type"] == "usage":
                    round_usage = chunk

        finally:
            if live:
                live.stop()
                live = None

        # 累计 token
        total_input += round_usage.get("input_tokens", 0)
        total_output += round_usage.get("output_tokens", 0)

        if tool_calls:
            # 有工具调用
            assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
            if text_buf:
                assistant_msg["content"] = text_buf
            if thinking_buf:
                assistant_msg["reasoning_content"] = thinking_buf
            messages.append(assistant_msg)

            for call in tool_calls:
                name = call["name"]
                args = call["args"]
                call_id = call["id"]
                used_tools_this_turn.append((name, args))

                if check_doom_loop(_recent_calls, name, args):
                    logger.warning("doom-loop detected: %s", name)
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": name,
                        "content": json.dumps({"error": "doom-loop: 同参数重复调用3次，已中止"}, ensure_ascii=False),
                    })
                    tool_calls = None
                    break

                if on_tool_call:
                    on_tool_call(name, args)

                result = tools.execute(name, args)

                if on_tool_result:
                    on_tool_result(name, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            # 继续下一轮
            continue

        if (
            missing_required_tool(expectation, used_tools_this_turn)
            and incomplete_tool_retries < MAX_INCOMPLETE_TOOL_RETRIES
        ):
            retry_prompt = build_retry_user_message(expectation, text_buf)
            incomplete_tool_retries += 1
            logger.info(
                "loop_guard retry=%d required_tool=%s reason=%s",
                incomplete_tool_retries,
                expectation.required_tool if expectation else "",
                expectation.reason if expectation else "",
            )
            if text_buf:
                _retry_msg: dict[str, Any] = {"role": "assistant", "content": text_buf}
                if thinking_buf:
                    _retry_msg["reasoning_content"] = thinking_buf
                messages.append(_retry_msg)
            messages.append({"role": "user", "content": retry_prompt})
            if console:
                console.print("  [yellow]⚠ 检测到模型未执行必需工具，已自动要求继续执行[/yellow]")
            continue

        # 纯文本回答 — 完成
        if missing_required_tool(expectation, used_tools_this_turn):
            warning = build_retry_exhausted_warning(expectation, incomplete_tool_retries)
            text_buf = f"{warning}\n\n{text_buf}".strip()
        _final_msg: dict[str, Any] = {"role": "assistant", "content": text_buf}
        if thinking_buf:
            _final_msg["reasoning_content"] = thinking_buf
        messages.append(_final_msg)
        elapsed = time.monotonic() - t_start
        return {
            "text": text_buf,
            "streamed": streamed,
            "usage": {"input_tokens": total_input, "output_tokens": total_output},
            "elapsed": elapsed,
        }

    return {
        "text": "(Agent 工具调用轮次超限，已停止)",
        "usage": {"input_tokens": total_input, "output_tokens": total_output},
        "elapsed": time.monotonic() - t_start,
    }
