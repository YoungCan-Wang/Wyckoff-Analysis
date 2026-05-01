# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy

from cli.sub_agents import (
    ANALYSIS_AGENT,
    RESEARCH_AGENT,
    TRADING_AGENT,
    SubAgentToolProxy,
    run_sub_agent,
)
from cli.tools import TOOL_SCHEMAS
from tests.helpers.agent_loop_harness import ScriptedProvider, StubToolRegistry


# ---------------------------------------------------------------------------
# SubAgentToolProxy 过滤测试
# ---------------------------------------------------------------------------

class TestSubAgentToolProxy:
    def test_schemas_only_returns_allowed(self):
        registry = StubToolRegistry(schemas=deepcopy(TOOL_SCHEMAS))
        allowed = {"analyze_stock", "portfolio"}
        proxy = SubAgentToolProxy(registry, allowed)

        names = {s["name"] for s in proxy.schemas()}
        assert names == allowed

    def test_execute_allowed_tool(self):
        registry = StubToolRegistry(tool_results={"analyze_stock": {"health": "OK"}})
        proxy = SubAgentToolProxy(registry, {"analyze_stock"})

        result = proxy.execute("analyze_stock", {"code": "000001"})
        assert result == {"health": "OK"}
        assert registry.calls[0]["name"] == "analyze_stock"

    def test_execute_blocked_tool_returns_error(self):
        registry = StubToolRegistry()
        proxy = SubAgentToolProxy(registry, {"analyze_stock"})

        result = proxy.execute("update_portfolio", {"action": "add"})
        assert "error" in result
        assert "无权" in result["error"]
        assert len(registry.calls) == 0


# ---------------------------------------------------------------------------
# SubAgent 定义一致性
# ---------------------------------------------------------------------------

def test_agent_tool_names_exist_in_schemas():
    schema_names = {s["name"] for s in TOOL_SCHEMAS}
    for agent in (RESEARCH_AGENT, ANALYSIS_AGENT, TRADING_AGENT):
        missing = set(agent.tool_names) - schema_names
        assert not missing, f"{agent.name} references unknown tools: {missing}"


# ---------------------------------------------------------------------------
# run_sub_agent 集成测试
# ---------------------------------------------------------------------------

def test_run_sub_agent_basic():
    provider = ScriptedProvider([
        [
            {"type": "text_delta", "text": "大盘水温偏暖，上证涨 0.5%。"},
            {"type": "usage", "input_tokens": 50, "output_tokens": 15},
        ],
    ])
    registry = StubToolRegistry()

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="查看大盘水温",
        context="",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "research"
    assert "大盘水温偏暖" in result["result"]
    assert result["usage"]["output_tokens"] == 15


def test_run_sub_agent_with_tool_call():
    provider = ScriptedProvider([
        [
            {
                "type": "tool_calls",
                "tool_calls": [{"id": "tc1", "name": "get_market_overview", "args": {}}],
                "text": "",
            },
            {"type": "usage", "input_tokens": 30, "output_tokens": 5},
        ],
        [
            {"type": "text_delta", "text": "上证指数涨 0.3%，市场偏暖。"},
            {"type": "usage", "input_tokens": 60, "output_tokens": 12},
        ],
    ])
    registry = StubToolRegistry(
        tool_results={"get_market_overview": {"sh": "+0.3%", "sz": "+0.1%"}}
    )

    result = run_sub_agent(
        RESEARCH_AGENT,
        task="查看大盘水温",
        context="用户想了解市场环境",
        provider=provider,
        registry=registry,
    )

    assert result["agent"] == "research"
    assert "上证" in result["result"]
    assert registry.calls[0]["name"] == "get_market_overview"
