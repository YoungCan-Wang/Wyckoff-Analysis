# -*- coding: utf-8 -*-
"""Wyckoff MCP Server — 将 Wyckoff 分析能力通过 MCP 协议对外暴露。"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("wyckoff")


# ---------------------------------------------------------------------------
# 全局 ToolContext — 从环境变量构建凭证
# ---------------------------------------------------------------------------

def _build_ctx():
    from cli.tools import ToolContext
    return ToolContext(state={
        "user_id": os.getenv("SUPABASE_USER_ID", ""),
        "access_token": os.getenv("SUPABASE_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("SUPABASE_REFRESH_TOKEN", ""),
    })


_ctx = _build_ctx()


# ---------------------------------------------------------------------------
# Tier 1: 无需凭证 — 纯本地 SQLite 读取
# ---------------------------------------------------------------------------

from agents.chat_tools import query_history as _query_history


@mcp.tool()
def query_history(source: str, status: str = "all", run_date: str = "", decision: str = "", limit: int = 20) -> dict:
    """查询历史记录：推荐追踪(recommendation)、信号确认池(signal)或尾盘买入(tail_buy)。"""
    return _query_history(source=source, status=status, run_date=run_date, decision=decision, limit=limit)


# ---------------------------------------------------------------------------
# Tier 2: 需 TUSHARE_TOKEN（env 注入）
# ---------------------------------------------------------------------------

from agents.chat_tools import (
    search_stock_by_name as _search_stock_by_name,
    analyze_stock as _analyze_stock,
    get_market_overview as _get_market_overview,
    screen_stocks as _screen_stocks,
    run_backtest as _run_backtest,
)


@mcp.tool()
def search_stock_by_name(keyword: str) -> list[dict]:
    """根据关键词搜索 A 股股票，支持名称、代码、拼音首字母模糊搜索。"""
    return _search_stock_by_name(keyword=keyword, tool_context=_ctx)


@mcp.tool()
def analyze_stock(code: str, mode: str = "diagnose", cost: float = 0.0, days: int = 30) -> dict:
    """分析单只 A 股：mode='diagnose' 做 Wyckoff 诊断，mode='price' 返回近期 OHLCV。"""
    return _analyze_stock(code=code, mode=mode, cost=cost, days=days, tool_context=_ctx)


@mcp.tool()
def get_market_overview() -> dict:
    """获取当前 A 股大盘环境概览（上证、深证、创业板指数）。"""
    return _get_market_overview(tool_context=_ctx)


@mcp.tool()
def screen_stocks(board: str = "all") -> dict:
    """运行 Wyckoff 五层漏斗筛选，从全市场筛选结构性机会股票。耗时较长。"""
    return _screen_stocks(board=board, tool_context=_ctx)


@mcp.tool()
def run_backtest(
    start: str = "",
    end: str = "",
    hold_days: int = 10,
    top_n: int = 3,
    board: str = "main_chinext",
    stop_loss_pct: float = -7.0,
    take_profit_pct: float = 18.0,
) -> dict:
    """回测威科夫五层漏斗策略的历史表现。耗时较长（3-10分钟）。"""
    return _run_backtest(
        start=start, end=end, hold_days=hold_days, top_n=top_n,
        board=board, stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
        tool_context=_ctx,
    )


# ---------------------------------------------------------------------------
# Tier 3: 需 Supabase 用户认证
# ---------------------------------------------------------------------------

from agents.chat_tools import (
    portfolio as _portfolio,
    update_portfolio as _update_portfolio,
    generate_ai_report as _generate_ai_report,
    generate_strategy_decision as _generate_strategy_decision,
)


@mcp.tool()
def portfolio(mode: str = "view") -> dict:
    """查看或诊断用户持仓。mode='view' 仅查看，mode='diagnose' 做 Wyckoff 诊断。"""
    return _portfolio(mode=mode, tool_context=_ctx)


@mcp.tool()
def update_portfolio(
    action: str,
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    table: str = "",
    codes: list[str] = None,
) -> dict:
    """管理持仓(add/update/remove/set_cash)或删除追踪记录(delete_records)。"""
    return _update_portfolio(
        action=action, code=code, name=name, shares=shares,
        cost_price=cost_price, buy_dt=buy_dt, free_cash=free_cash,
        table=table, codes=codes, tool_context=_ctx,
    )


@mcp.tool()
def generate_ai_report(stock_codes: list[str]) -> dict:
    """对指定股票列表生成威科夫三阵营 AI 深度研报。"""
    return _generate_ai_report(stock_codes=stock_codes, tool_context=_ctx)


@mcp.tool()
def generate_strategy_decision() -> dict:
    """生成持仓去留决策和新标的买入策略。"""
    return _generate_strategy_decision(tool_context=_ctx)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    from integrations.local_db import init_db
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
