"""LLM Provider 抽象接口 — 所有模型供应商实现这个接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from typing import Any


class LLMProvider(ABC):
    """
    统一 LLM 调用接口。

    每个 provider 把各自 SDK 的响应翻译成统一格式：
    - {"type": "text", "text": "..."}
    - {"type": "tool_calls", "tool_calls": [{"id", "name", "args"}]}

    流式接口 yield chunk：
    - {"type": "text_delta", "text": "..."}
    - {"type": "tool_calls", "tool_calls": [...]}   （流结束时一次性返回）
    - {"type": "usage", "input_tokens": N, "output_tokens": N}
    """

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        """流式调用，yield chunk 字典。"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 显示名称。"""
        ...
