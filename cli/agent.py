"""
Headless agent loop compatibility wrapper.

The canonical execution loop now lives in ``cli.runtime.AgentRuntime`` and
emits normalized runtime events. This module preserves the historical ``run``
API used by sub-agents and tests.
"""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown

from cli.providers.base import LLMProvider
from cli.runtime import AgentRuntime, RuntimeEvent
from cli.scratchpad import AgentScratchpad
from cli.tools import ToolRegistry


def run(
    provider: LLMProvider,
    tools: ToolRegistry,
    messages: list[dict[str, Any]],
    system_prompt: str = "",
    on_tool_call: callable = None,
    on_tool_result: callable = None,
    console=None,
    scratchpad: AgentScratchpad | None = None,
) -> dict[str, Any]:
    """
    执行一次完整的 Agent 循环。

    Returns
    -------
    {"text": str, "usage": {"input_tokens": int, "output_tokens": int}, "elapsed": float}
    """

    runtime = AgentRuntime(provider, tools, scratchpad=scratchpad)
    final: dict[str, Any] | None = None

    for event in runtime.run_stream(messages, system_prompt):
        _dispatch_legacy_callbacks(event, on_tool_call, on_tool_result)
        if event["type"] == "done":
            final = {
                "text": event["text"],
                "streamed": event.get("streamed", False),
                "usage": event.get("usage", {}),
                "elapsed": event.get("elapsed", 0.0),
            }
            if console and event["text"]:
                console.print(Markdown(event["text"]))

    return final or {
        "text": "(Agent 未返回内容)",
        "streamed": False,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "elapsed": 0.0,
    }


def _dispatch_legacy_callbacks(
    event: RuntimeEvent,
    on_tool_call: callable = None,
    on_tool_result: callable = None,
) -> None:
    """Translate runtime events to the legacy callback hooks."""

    event_type = event.get("type")
    if event_type == "tool_start" and on_tool_call:
        on_tool_call(event["name"], event["args"])
        return
    if event_type in {"tool_result", "tool_error"} and on_tool_result and "result" in event:
        on_tool_result(event["name"], event["result"])
