"""Sub-agent 基础设施 — SubAgent 定义、工具代理、运行函数、委派工具。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from cli.sub_agent_prompts import (
    ANALYSIS_AGENT_PROMPT,
    RESEARCH_AGENT_PROMPT,
    TRADING_AGENT_PROMPT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubAgent:
    name: str
    system_prompt: str
    tool_names: tuple[str, ...]
    description: str = ""


RESEARCH_AGENT = SubAgent(
    name="research",
    description="数据收集：全市场扫描、信号、推荐、回测",
    system_prompt=RESEARCH_AGENT_PROMPT,
    tool_names=(
        "search_stock_by_name",
        "analyze_stock",
        "get_market_overview",
        "query_history",
        "screen_stocks",
        "run_backtest",
        "check_background_tasks",
    ),
)

ANALYSIS_AGENT = SubAgent(
    name="analysis",
    description="深度分析：个股诊断、持仓体检、AI 研报",
    system_prompt=ANALYSIS_AGENT_PROMPT,
    tool_names=(
        "analyze_stock",
        "portfolio",
        "get_market_overview",
        "generate_ai_report",
    ),
)

TRADING_AGENT = SubAgent(
    name="trading",
    description="去留决策：攻防指令、调仓执行",
    system_prompt=TRADING_AGENT_PROMPT,
    tool_names=(
        "portfolio",
        "update_portfolio",
        "generate_strategy_decision",
        "analyze_stock",
        "get_market_overview",
    ),
)


class SubAgentToolProxy:
    """限制 sub-agent 只能看到/调用指定工具子集。"""

    def __init__(self, registry, allowed: set[str]):
        self._registry = registry
        self._allowed = allowed

    def schemas(self) -> list[dict[str, Any]]:
        return [s for s in self._registry.schemas() if s["name"] in self._allowed]

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self._allowed:
            return {"error": f"sub-agent 无权调用工具: {name}"}
        return self._registry.execute(name, args)

    def concurrency_safe(self, name: str) -> bool:
        if name not in self._allowed:
            return False
        return self._registry.concurrency_safe(name)


def run_sub_agent(
    sub: SubAgent,
    task: str,
    context: str,
    provider,
    registry,
) -> dict[str, Any]:
    """启动一个 sub-agent mini loop，返回结果文本。"""
    from cli.agent import run as agent_run
    from core.prompts import with_current_time

    proxy = SubAgentToolProxy(registry, set(sub.tool_names))
    user_content = task
    if context:
        user_content = f"{task}\n\n上下文:\n{context}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    result = agent_run(provider, proxy, messages, with_current_time(sub.system_prompt))
    return {"agent": sub.name, "result": result["text"], "usage": result.get("usage", {})}


# ---------------------------------------------------------------------------
# 委派工具函数 — 注册为 Orchestrator 可调用的工具
# ---------------------------------------------------------------------------


def delegate_to_research(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派研究员收集数据。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return {"error": "provider/registry 未注入，无法启动 sub-agent"}
    return run_sub_agent(RESEARCH_AGENT, task, context, provider, registry)


def delegate_to_analysis(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派分析师做深度分析。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return {"error": "provider/registry 未注入，无法启动 sub-agent"}
    return run_sub_agent(ANALYSIS_AGENT, task, context, provider, registry)


def delegate_to_trading(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派交易员做去留决策。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return {"error": "provider/registry 未注入，无法启动 sub-agent"}
    return run_sub_agent(TRADING_AGENT, task, context, provider, registry)
