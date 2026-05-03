"""Claude Provider — anthropic SDK 实现。"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import anthropic

from cli.providers.base import LLMProvider


class ClaudeProvider(LLMProvider):
    """通过 anthropic SDK 调用 Claude 模型。"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    @property
    def name(self) -> str:
        return f"Claude ({self._model})"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]:
        # 构建 Claude messages
        claude_messages = self._build_messages(messages)

        # 构建工具声明
        claude_tools = self._build_tools(tools) if tools else []

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": claude_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if claude_tools:
            kwargs["tools"] = claude_tools

        response = self._client.messages.create(**kwargs)
        return self._parse_response(response)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        claude_messages = self._build_messages(messages)
        claude_tools = self._build_tools(tools) if tools else []

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": claude_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if claude_tools:
            kwargs["tools"] = claude_tools

        tool_calls = []
        text_buf = ""
        # 用于追踪当前正在构建的 tool_use block
        current_tool: dict | None = None
        current_tool_json = ""
        input_tokens = 0
        output_tokens = 0

        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool = {"id": block.id, "name": block.name, "args": {}}
                        current_tool_json = ""
                    elif block.type == "text":
                        pass
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        text_buf += delta.text
                        yield {"type": "text_delta", "text": delta.text}
                    elif delta.type == "input_json_delta":
                        current_tool_json += delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool is not None:
                        try:
                            current_tool["args"] = json.loads(current_tool_json) if current_tool_json else {}
                        except json.JSONDecodeError:
                            current_tool["args"] = {}
                        tool_calls.append(current_tool)
                        current_tool = None
                        current_tool_json = ""
                elif event.type == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage:
                        output_tokens = getattr(usage, "output_tokens", 0)
                elif event.type == "message_start":
                    usage = getattr(event.message, "usage", None)
                    if usage:
                        input_tokens = getattr(usage, "input_tokens", 0)

        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls, "text": text_buf}

        yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}

    def _build_messages(self, messages: list[dict]) -> list[dict]:
        """将统一消息格式转为 Claude messages 格式。"""
        claude_msgs = []
        for msg in messages:
            role = msg["role"]

            if role == "user":
                claude_msgs.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                content = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg.get("tool_calls", []):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["args"],
                        }
                    )
                claude_msgs.append({"role": "assistant", "content": content})

            elif role == "tool":
                result = msg["content"]
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                claude_msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": result,
                            }
                        ],
                    }
                )

        return claude_msgs

    def _build_tools(self, tools: list[dict]) -> list[dict]:
        """将标准 function schema 转为 Claude tools 格式。"""
        claude_tools = []
        for t in tools:
            claude_tools.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return claude_tools

    def _parse_response(self, response) -> dict[str, Any]:
        """解析 Claude 响应为统一格式。"""
        tool_calls = []
        text_parts = []

        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "args": block.input,
                    }
                )
            elif block.type == "text":
                text_parts.append(block.text)

        if tool_calls:
            return {
                "type": "tool_calls",
                "tool_calls": tool_calls,
                "text": "".join(text_parts),  # Claude 可能同时返回文本和工具调用
            }

        return {"type": "text", "text": "".join(text_parts)}
