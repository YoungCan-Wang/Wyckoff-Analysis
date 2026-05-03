"""Gemini Provider — google-genai SDK 实现。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

from google import genai
from google.genai import types

from cli.providers.base import LLMProvider


class GeminiProvider(LLMProvider):
    """通过 google-genai SDK 调用 Gemini 模型。"""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    @property
    def name(self) -> str:
        return f"Gemini ({self._model})"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]:
        # 构建 Gemini contents
        contents = self._build_contents(messages)

        # 构建工具声明
        gemini_tools = self._build_tools(tools) if tools else None

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            tools=gemini_tools,
        )

        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )

        return self._parse_response(response)

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        contents = self._build_contents(messages)
        gemini_tools = self._build_tools(tools) if tools else None

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            tools=gemini_tools,
        )

        text_buf = ""
        tool_calls = []
        usage_meta = None

        for chunk in self._client.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        ):
            # usage_metadata 可能出现在任意 chunk（尤其是最后一个无 candidates 的 chunk）
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:
                usage_meta = um
            if not chunk.candidates:
                continue
            for part in chunk.candidates[0].content.parts:
                if part.function_call:
                    fc = part.function_call
                    tool_calls.append(
                        {
                            "id": uuid.uuid4().hex[:12],
                            "name": fc.name,
                            "args": dict(fc.args) if fc.args else {},
                        }
                    )
                elif part.text:
                    text_buf += part.text
                    yield {"type": "text_delta", "text": part.text}

        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls, "text": text_buf}

        yield {
            "type": "usage",
            "input_tokens": getattr(usage_meta, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(usage_meta, "candidates_token_count", 0) or 0,
        }

    def _build_contents(self, messages: list[dict]) -> list[types.Content]:
        """将统一消息格式转为 Gemini Content 列表。"""
        contents = []
        for msg in messages:
            role = msg["role"]

            if role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=msg["content"])],
                    )
                )

            elif role == "assistant":
                parts = []
                # 文本部分
                if msg.get("content"):
                    parts.append(types.Part.from_text(text=msg["content"]))
                # 工具调用部分
                for tc in msg.get("tool_calls", []):
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=tc["name"],
                                args=tc["args"],
                            )
                        )
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))

            elif role == "tool":
                # Gemini 要求 function_response 在一个 Content 里
                result = msg["content"]
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result = {"result": result}
                # Gemini FunctionResponse.response 必须是 dict，不能是 list
                if not isinstance(result, dict):
                    result = {"result": result}
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=msg.get("name", "unknown"),
                                    response=result,
                                )
                            )
                        ],
                    )
                )

        return contents

    def _build_tools(self, tools: list[dict]) -> list[types.Tool]:
        """将标准 function schema 转为 Gemini Tool 格式。"""
        declarations = []
        for t in tools:
            params = t.get("parameters", {})
            declarations.append(
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=params if params.get("properties") else None,
                )
            )
        return [types.Tool(function_declarations=declarations)]

    def _parse_response(self, response) -> dict[str, Any]:
        """解析 Gemini 响应为统一格式。"""
        if not response.candidates:
            return {"type": "text", "text": "(模型未返回内容)"}

        parts = response.candidates[0].content.parts
        tool_calls = []
        text_parts = []

        for part in parts:
            if part.function_call:
                fc = part.function_call
                tool_calls.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "name": fc.name,
                        "args": dict(fc.args) if fc.args else {},
                    }
                )
            elif part.text:
                text_parts.append(part.text)

        if tool_calls:
            return {"type": "tool_calls", "tool_calls": tool_calls, "text": "".join(text_parts)}

        return {"type": "text", "text": "".join(text_parts)}
