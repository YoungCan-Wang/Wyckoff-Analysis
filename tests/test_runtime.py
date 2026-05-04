from __future__ import annotations

from cli.runtime import AgentRuntime, partition_tool_calls
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry


def test_partition_tool_calls_uses_concurrency_metadata():
    calls = [
        {"id": "tc_a", "name": "fast_a", "args": {}},
        {"id": "tc_b", "name": "fast_b", "args": {}},
        {"id": "tc_c", "name": "write_file", "args": {}},
        {"id": "tc_d", "name": "fast_c", "args": {}},
    ]

    batches = partition_tool_calls(calls, {"fast_a", "fast_b", "fast_c"}.__contains__)

    assert [batch["concurrent"] for batch in batches] == [True, False, True]
    assert [[call["name"] for call in batch["calls"]] for batch in batches] == [
        ["fast_a", "fast_b"],
        ["write_file"],
        ["fast_c"],
    ]


def test_runtime_emits_tool_events_and_done():
    provider = ScriptedProvider(
        rounds=[
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 10, "output_tokens": 3},
            ],
            [
                {"type": "text_delta", "text": "你当前没有持仓。"},
                {"type": "usage", "input_tokens": 15, "output_tokens": 8},
            ],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [{"role": "user", "content": "我的持仓有什么"}]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    assert [e["type"] for e in events if e["type"].startswith("tool_")] == ["tool_calls", "tool_start", "tool_result"]
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "你当前没有持仓。"
    assert events[-1]["usage"] == {"input_tokens": 25, "output_tokens": 11}
    assert any(m.get("role") == "tool" and m.get("name") == "portfolio" for m in messages)


def test_runtime_emits_retry_event_when_required_tool_is_skipped():
    provider = ScriptedProvider(
        rounds=[
            [
                {"type": "text_delta", "text": "我先说下计划。"},
                {"type": "usage", "input_tokens": 5, "output_tokens": 4},
            ],
            [
                {
                    "type": "tool_calls",
                    "tool_calls": [{"id": "tc_diag", "name": "portfolio", "args": {"mode": "diagnose"}}],
                    "text": "",
                },
                {"type": "usage", "input_tokens": 8, "output_tokens": 3},
            ],
            [
                {"type": "text_delta", "text": "体检完成。"},
                {"type": "usage", "input_tokens": 12, "output_tokens": 5},
            ],
        ]
    )
    tools = StubToolRegistry(tool_results={"portfolio": {"positions": []}})
    messages = [
        {"role": "user", "content": "我的持仓有什么"},
        {"role": "assistant", "content": "你手里现在有 4 张牌。"},
        {"role": "user", "content": "做一下体检"},
    ]

    events = list(AgentRuntime(provider, tools).run_stream(messages))

    retries = [e for e in events if e["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["required_tool"] == "portfolio"
    assert "不要重复计划" in retries[0]["message"]
    assert events[-1]["text"] == "体检完成。"
