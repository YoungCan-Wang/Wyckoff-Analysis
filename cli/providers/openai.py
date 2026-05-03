"""OpenAI Provider — openai SDK 实现。"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx
import openai

from cli.providers.base import LLMProvider

_TIMEOUT = httpx.Timeout(300.0, connect=60.0)


class OpenAIProvider(LLMProvider):
    """通过 openai SDK 调用 OpenAI 模型。"""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str = ""):
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": _TIMEOUT}
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self._client = openai.OpenAI(**kwargs)
        self._model = model

    @property
    def name(self) -> str:
        return f"OpenAI ({self._model})"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]:
        # 构建 OpenAI messages
        oai_messages = self._build_messages(messages, system_prompt)

        # 构建工具声明
        oai_tools = self._build_tools(tools) if tools else None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "frequency_penalty": 0.3,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        result = self._parse_response(response)
        if hasattr(response, "usage") and response.usage:
            result["usage"] = {
                "input_tokens": response.usage.prompt_tokens or 0,
                "output_tokens": response.usage.completion_tokens or 0,
            }
        return result

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        oai_messages = self._build_messages(messages, system_prompt)
        oai_tools = self._build_tools(tools) if tools else None

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "frequency_penalty": 0.3,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        tool_map: dict[int, dict] = {}  # index → {id, name, args_json}
        text_buf = ""
        input_tokens = 0
        output_tokens = 0

        try:
            stream = self._client.chat.completions.create(**kwargs)
        except Exception:
            # 某些第三方端点不支持 stream_options 或 tool_choice/frequency_penalty，逐个去掉后重试
            kwargs.pop("stream_options", None)
            try:
                stream = self._client.chat.completions.create(**kwargs)
            except Exception:
                kwargs.pop("tool_choice", None)
                kwargs.pop("frequency_penalty", None)
                stream = self._client.chat.completions.create(**kwargs)

        for chunk in stream:
            if not chunk.choices and chunk.usage:
                input_tokens = chunk.usage.prompt_tokens or 0
                output_tokens = chunk.usage.completion_tokens or 0
                continue

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # 推理模型（DeepSeek R1 / LongCat-Thinking 等）的思考过程
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"type": "thinking_delta", "text": reasoning}

            if delta.content:
                text_buf += delta.content
                yield {"type": "text_delta", "text": delta.content}

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_map:
                        tool_map[idx] = {
                            "id": tc_delta.id or "",
                            "name": tc_delta.function.name or "" if tc_delta.function else "",
                            "args_json": "",
                        }
                    if tc_delta.id:
                        tool_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_map[idx]["args_json"] += tc_delta.function.arguments

        # 兜底：某些模型（如 kimi-k2 via NVIDIA）会把 tool call 输出为文本标签
        if not tool_map and "<tool_call>" in text_buf:
            import re

            for m in re.finditer(
                r"<tool_call>\s*\{.*?\"name\"\s*:\s*\"([^\"]+)\".*?\"arguments\"\s*:\s*(\{.*?\})\s*\}\s*</tool_call>",
                text_buf,
                re.DOTALL,
            ):
                fname, fargs_str = m.group(1), m.group(2)
                try:
                    fargs = json.loads(fargs_str)
                except json.JSONDecodeError:
                    fargs = {}
                tool_map[len(tool_map)] = {
                    "id": f"text_tc_{len(tool_map)}",
                    "name": fname,
                    "args_json": json.dumps(fargs, ensure_ascii=False),
                }
            if tool_map:
                # 移除文本中的 tool_call 标签
                text_buf = re.sub(r"<tool_call>.*?</tool_call>", "", text_buf, flags=re.DOTALL).strip()

        if tool_map:
            tool_calls = []
            for idx in sorted(tool_map):
                entry = tool_map[idx]
                try:
                    args = json.loads(entry["args_json"]) if entry["args_json"] else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({"id": entry["id"], "name": entry["name"], "args": args})
            yield {"type": "tool_calls", "tool_calls": tool_calls, "text": text_buf}

        yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}

    def _build_messages(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """将统一消息格式转为 OpenAI messages 格式。"""
        oai_msgs = []
        if system_prompt:
            oai_msgs.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg["role"]

            if role == "user":
                oai_msgs.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                oai_msg: dict[str, Any] = {"role": "assistant"}
                if msg.get("content"):
                    oai_msg["content"] = msg["content"]
                if msg.get("reasoning_content"):
                    oai_msg["reasoning_content"] = msg["reasoning_content"]
                if msg.get("tool_calls"):
                    oai_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"], ensure_ascii=False),
                            },
                        }
                        for tc in msg["tool_calls"]
                    ]
                oai_msgs.append(oai_msg)

            elif role == "tool":
                result = msg["content"]
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                oai_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": result,
                    }
                )

        return oai_msgs

    def _build_tools(self, tools: list[dict]) -> list[dict]:
        """将标准 function schema 转为 OpenAI tools 格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    def _parse_response(self, response) -> dict[str, Any]:
        """解析 OpenAI 响应为统一格式。"""
        choice = response.choices[0]
        message = choice.message

        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "args": args,
                    }
                )
            return {
                "type": "tool_calls",
                "tool_calls": tool_calls,
                "text": message.content or "",
            }

        return {"type": "text", "text": message.content or ""}
