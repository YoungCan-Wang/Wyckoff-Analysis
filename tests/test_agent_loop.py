# -*- coding: utf-8 -*-
from __future__ import annotations

from cli.loop_guard import check_doom_loop, resolve_turn_expectation
from cli.tools import CONFIRM_TOOLS, ToolRegistry
from tests.helpers.agent_loop_harness import AgentLoopHarness


def test_resolve_turn_expectation_uses_portfolio_context_for_followup_checkup():
    messages = [
        {"role": "user", "content": "我的持仓有什么"},
        {
            "role": "assistant",
            "content": "你手里现在有 4 张牌，外加 1.16 万现金。\n\n| 代码 | 名称 | 持股 | 成本价 | 买入日 |",
        },
        {"role": "user", "content": "做一下体检"},
    ]

    expectation = resolve_turn_expectation(messages)

    assert expectation is not None
    assert expectation.required_tool == "portfolio"


def test_resolve_turn_expectation_handles_affirmative_followup_after_portfolio_invite():
    messages = [
        {"role": "user", "content": "你看看我最新的持仓是啥"},
        {
            "role": "assistant",
            "content": "你当前有 5 只持仓，现金 29,755.63 元。要我对这5只票做个全面体检吗？",
        },
        {"role": "user", "content": "要的"},
    ]

    expectation = resolve_turn_expectation(messages)

    assert expectation is not None
    assert expectation.required_tool == "portfolio"


def test_resolve_turn_expectation_handles_portfolio_daily_trend_followup():
    messages = [
        {"role": "user", "content": "你看看我最新的持仓是啥"},
        {
            "role": "assistant",
            "content": "你当前有 5 只持仓，现金 29,755.63 元。",
        },
        {"role": "user", "content": "你根据过去的日线分析一下这几个股票的未来走势吧"},
    ]

    expectation = resolve_turn_expectation(messages)

    assert expectation is not None
    assert expectation.required_tool == "portfolio"


def test_resolve_turn_expectation_does_not_hijack_explicit_stock_after_portfolio_context():
    messages = [
        {"role": "user", "content": "跟我的持仓股票做一下未来的预测"},
        {
            "role": "assistant",
            "content": "5只票全部诊断完成，数据截至 2026-04-30。",
        },
        {"role": "user", "content": "分析一下海德股份"},
    ]

    expectation = resolve_turn_expectation(messages)

    assert expectation is None


def test_agent_loop_retries_planning_only_portfolio_turn_until_tool_executes():
    def second_round(messages, tools, system_prompt):
        assert messages[-1]["role"] == "user"
        assert "portfolio" in messages[-1]["content"]
        assert "不要重复计划" in messages[-1]["content"]
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_diag", "name": "portfolio", "args": {"mode": "diagnose"}}],
                "text": "",
            },
            {"type": "usage", "input_tokens": 30, "output_tokens": 5},
        ]

    def third_round(messages, tools, system_prompt):
        assert any(m.get("role") == "tool" and m.get("name") == "portfolio" for m in messages)
        return [
            {"type": "text_delta", "text": "持仓体检已完成：金螳螂偏弱，苏州银行可继续观察。"},
            {"type": "usage", "input_tokens": 40, "output_tokens": 18},
        ]

    harness = AgentLoopHarness(
        rounds=[
            [
                {"type": "text_delta", "text": "计划\n1. 逐只体检你的 4 只持仓\n2. 汇总去留建议\n现在开第一刀。"},
                {"type": "usage", "input_tokens": 20, "output_tokens": 12},
            ],
            second_round,
            third_round,
        ],
        tool_results={
            "portfolio": {
                "message": "mock portfolio diagnosis",
                "positions": [{"code": "002081", "health": "WEAK"}],
            }
        },
    )

    outcome = harness.run_turn(
        [
            {"role": "user", "content": "我的持仓有什么"},
            {"role": "assistant", "content": "你手里现在有 4 张牌，外加 1.16 万现金。"},
            {"role": "user", "content": "做一下体检"},
        ]
    )

    assert outcome["result"]["text"] == "持仓体检已完成：金螳螂偏弱，苏州银行可继续观察。"
    assert [call["name"] for call in outcome["tool_calls"]] == ["portfolio"]
    assert len(outcome["provider_calls"]) == 3
    assert outcome["messages"][-1]["role"] == "assistant"
    assert "持仓体检已完成" in outcome["messages"][-1]["content"]


def test_agent_loop_retries_hallucinated_portfolio_list_until_portfolio_runs():
    def second_round(messages, tools, system_prompt):
        assert messages[-1]["role"] == "user"
        assert "portfolio" in messages[-1]["content"]
        return [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc_pf", "name": "portfolio", "args": {"mode": "view"}}],
                "text": "",
            },
            {"type": "usage", "input_tokens": 18, "output_tokens": 4},
        ]

    harness = AgentLoopHarness(
        rounds=[
            [
                {"type": "text_delta", "text": "你大概有几只股票和一些现金，我先给你总结一下。"},
                {"type": "usage", "input_tokens": 15, "output_tokens": 11},
            ],
            second_round,
            [
                {"type": "text_delta", "text": "你当前有 4 只持仓，现金 1.16 万。"},
                {"type": "usage", "input_tokens": 24, "output_tokens": 9},
            ],
        ],
        tool_results={"portfolio": {"positions": [1, 2, 3, 4], "free_cash": 11600}},
    )

    outcome = harness.run_turn([{"role": "user", "content": "我的持仓有什么"}])

    assert outcome["result"]["text"] == "你当前有 4 只持仓，现金 1.16 万。"
    assert [call["name"] for call in outcome["tool_calls"]] == ["portfolio"]


def test_agent_loop_does_not_retry_non_mandatory_plain_text_turn():
    harness = AgentLoopHarness(
        rounds=[
            [
                {"type": "text_delta", "text": "威科夫核心是供需与主力行为。"},
                {"type": "usage", "input_tokens": 8, "output_tokens": 7},
            ]
        ]
    )

    outcome = harness.run_turn([{"role": "user", "content": "简单讲讲威科夫方法"}])

    assert outcome["result"]["text"] == "威科夫核心是供需与主力行为。"
    assert outcome["tool_calls"] == []
    assert len(outcome["provider_calls"]) == 1


def test_agent_loop_warns_after_retry_budget_is_exhausted():
    harness = AgentLoopHarness(
        rounds=[
            [
                {"type": "text_delta", "text": "计划\n1. 先体检\n2. 再总结"},
                {"type": "usage", "input_tokens": 10, "output_tokens": 6},
            ],
            [
                {"type": "text_delta", "text": "我先给你说说思路。"},
                {"type": "usage", "input_tokens": 12, "output_tokens": 5},
            ],
            [
                {"type": "text_delta", "text": "还是先说计划，不着急执行。"},
                {"type": "usage", "input_tokens": 14, "output_tokens": 5},
            ],
        ]
    )

    outcome = harness.run_turn(
        [
            {"role": "user", "content": "我的持仓有什么"},
            {"role": "assistant", "content": "你手里现在有 4 张牌。"},
            {"role": "user", "content": "做一下体检"},
        ]
    )

    assert "连续 2 次没有调用必需工具" in outcome["result"]["text"]
    assert outcome["tool_calls"] == []
    assert len(outcome["provider_calls"]) == 3


# ---------------------------------------------------------------------------
# Doom-loop detection
# ---------------------------------------------------------------------------

class TestCheckDoomLoop:
    def test_no_trigger_below_threshold(self):
        recent: list[tuple[str, int]] = []
        assert not check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        assert not check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        assert len(recent) == 2

    def test_triggers_at_threshold(self):
        recent: list[tuple[str, int]] = []
        check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        assert check_doom_loop(recent, "analyze_stock", {"code": "000001"})

    def test_different_args_no_trigger(self):
        recent: list[tuple[str, int]] = []
        check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        check_doom_loop(recent, "analyze_stock", {"code": "000002"})
        assert not check_doom_loop(recent, "analyze_stock", {"code": "000001"})

    def test_window_eviction(self):
        recent: list[tuple[str, int]] = []
        check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        check_doom_loop(recent, "analyze_stock", {"code": "000001"})
        for i in range(5):
            check_doom_loop(recent, "screen_stocks", {"idx": i})
        assert not check_doom_loop(recent, "analyze_stock", {"code": "000001"})

    def test_agent_loop_breaks_on_doom_loop(self):
        harness = AgentLoopHarness(
            rounds=[
                [
                    {
                        "type": "tool_calls",
                        "tool_calls": [{"id": "tc1", "name": "analyze_stock", "args": {"code": "000001"}}],
                        "text": "",
                    },
                    {"type": "usage", "input_tokens": 10, "output_tokens": 3},
                ],
                [
                    {
                        "type": "tool_calls",
                        "tool_calls": [{"id": "tc2", "name": "analyze_stock", "args": {"code": "000001"}}],
                        "text": "",
                    },
                    {"type": "usage", "input_tokens": 10, "output_tokens": 3},
                ],
                [
                    {
                        "type": "tool_calls",
                        "tool_calls": [{"id": "tc3", "name": "analyze_stock", "args": {"code": "000001"}}],
                        "text": "",
                    },
                    {"type": "usage", "input_tokens": 10, "output_tokens": 3},
                ],
                [
                    {"type": "text_delta", "text": "已中止。"},
                    {"type": "usage", "input_tokens": 10, "output_tokens": 2},
                ],
            ],
            tool_results={"analyze_stock": {"price": 10.5}},
        )

        outcome = harness.run_turn([{"role": "user", "content": "查一下 000001 价格"}])

        doom_msgs = [m for m in outcome["messages"] if m.get("role") == "tool" and "doom-loop" in m.get("content", "")]
        assert len(doom_msgs) == 1


# ---------------------------------------------------------------------------
# Tool confirm callback
# ---------------------------------------------------------------------------

class TestToolConfirm:
    def test_confirm_tools_constant(self):
        assert "exec_command" in CONFIRM_TOOLS
        assert "write_file" in CONFIRM_TOOLS
        assert "update_portfolio" in CONFIRM_TOOLS
        assert "portfolio" not in CONFIRM_TOOLS

    def test_deny_blocks_execution(self):
        registry = ToolRegistry()
        registry.set_confirm_callback(lambda name, args: {"action": "deny"})
        result = registry.execute("exec_command", {"command": "echo hi"})
        assert "error" in result
        assert "拒绝" in result["error"]

    def test_always_skips_subsequent_confirms(self):
        calls = []
        def _confirm(name, args):
            calls.append(name)
            return {"action": "always"}
        registry = ToolRegistry()
        registry.set_confirm_callback(_confirm)
        registry.execute("exec_command", {"command": "echo 1"})
        registry.execute("exec_command", {"command": "echo 2"})
        assert len(calls) == 1

    def test_no_callback_skips_confirm(self):
        registry = ToolRegistry()
        result = registry.execute("exec_command", {"command": "echo hi"})
        assert "error" not in result or "拒绝" not in result.get("error", "")
