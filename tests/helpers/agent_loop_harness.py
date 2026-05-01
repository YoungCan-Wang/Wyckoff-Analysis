# -*- coding: utf-8 -*-
"""Deterministic harness for agent loop tests."""
from __future__ import annotations

from collections.abc import Callable, Generator
from copy import deepcopy
from typing import Any

from cli.agent import run
from cli.providers.base import LLMProvider


Chunk = dict[str, Any]
RoundScript = list[Chunk] | Callable[[list[dict[str, Any]], list[dict[str, Any]], str], list[Chunk]]


class ScriptedProvider(LLMProvider):
    """Replay scripted stream chunks round by round."""

    def __init__(self, rounds: list[RoundScript], name: str = "ScriptedProvider"):
        self._rounds = rounds
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def chat(self, messages, tools, system_prompt="") -> dict[str, Any]:
        raise NotImplementedError("ScriptedProvider only supports chat_stream() in loop tests")

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        round_idx = len(self.calls)
        if round_idx >= len(self._rounds):
            raise AssertionError(f"Unexpected extra provider round: {round_idx}")

        snapshot = {
            "round_idx": round_idx,
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "system_prompt": system_prompt,
        }
        self.calls.append(snapshot)

        script = self._rounds[round_idx]
        chunks = script(messages, tools, system_prompt) if callable(script) else script
        for chunk in chunks:
            yield deepcopy(chunk)


class StubToolRegistry:
    """Minimal ToolRegistry compatible stub."""

    def __init__(
        self,
        *,
        schemas: list[dict[str, Any]] | None = None,
        tool_results: dict[str, Any] | None = None,
    ):
        self._schemas = deepcopy(schemas) if schemas is not None else [
            {
                "name": "portfolio",
                "description": "Mock portfolio tool",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
        self._tool_results = tool_results or {}
        self.calls: list[dict[str, Any]] = []

    def schemas(self) -> list[dict[str, Any]]:
        return deepcopy(self._schemas)

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append({"name": name, "args": deepcopy(args)})
        result = self._tool_results.get(name, {"ok": True, "name": name, "args": deepcopy(args)})
        if callable(result):
            return result(name, deepcopy(args))
        return deepcopy(result)


class AgentLoopHarness:
    """Run a single turn through cli.agent.run() with scripted dependencies."""

    def __init__(
        self,
        *,
        rounds: list[RoundScript],
        tool_results: dict[str, Any] | None = None,
    ):
        self.provider = ScriptedProvider(rounds)
        self.tools = StubToolRegistry(tool_results=tool_results)

    def run_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
    ) -> dict[str, Any]:
        working_messages = deepcopy(messages)
        observed_tool_calls: list[dict[str, Any]] = []
        observed_tool_results: list[dict[str, Any]] = []
        result = run(
            provider=self.provider,
            tools=self.tools,
            messages=working_messages,
            system_prompt=system_prompt,
            on_tool_call=lambda name, args: observed_tool_calls.append(
                {"name": name, "args": deepcopy(args)}
            ),
            on_tool_result=lambda name, result: observed_tool_results.append(
                {"name": name, "result": deepcopy(result)}
            ),
        )
        return {
            "result": result,
            "messages": working_messages,
            "provider_calls": deepcopy(self.provider.calls),
            "tool_calls": observed_tool_calls,
            "tool_results": observed_tool_results,
            "tool_exec_calls": deepcopy(self.tools.calls),
        }
