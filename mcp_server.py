"""Wyckoff MCP Server — 将 Wyckoff 分析能力通过 MCP 协议对外暴露。"""

from __future__ import annotations

import os
from typing import Literal

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("wyckoff")


# ---------------------------------------------------------------------------
# 全局 ToolContext — 从环境变量构建凭证
# ---------------------------------------------------------------------------


def _build_ctx():
    from cli.tools import ToolContext

    state = {
        "user_id": os.getenv("SUPABASE_USER_ID", ""),
        "access_token": os.getenv("SUPABASE_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("SUPABASE_REFRESH_TOKEN", ""),
    }

    # 如果没有注入环境变量，尝试读取本地的 CLI 登录态
    if not state["access_token"]:
        from contextlib import suppress

        with suppress(Exception):
            from cli.auth import load_session

            sess = load_session()
            if sess:
                state["user_id"] = sess.get("user", {}).get("id", "")
                state["access_token"] = sess.get("access_token", "")
                state["refresh_token"] = sess.get("refresh_token", "")

    return ToolContext(state=state)


_ctx = _build_ctx()


# ---------------------------------------------------------------------------
# Tier 1: 无需凭证 — 纯本地 SQLite 读取
# ---------------------------------------------------------------------------

from agents.chat_tools import query_history as _query_history


@mcp.tool()
def query_history(
    source: Literal["recommendation", "signal", "tail_buy"],
    status: str = "all",
    run_date: str = "",
    decision: str = "",
    limit: int = 20,
) -> dict:
    """查询历史记录。

    **调用时机**：用户问"最近推荐了什么"、"信号池有哪些"、"尾盘买入记录"时调用。
    source 决定查哪张表：recommendation(形态复盘)、signal(信号确认池)、tail_buy(尾盘买入)。
    """
    return _query_history(source=source, status=status, run_date=run_date, decision=decision, limit=limit)


# ---------------------------------------------------------------------------
# Tier 2: 需 TUSHARE_TOKEN（env 注入）
# ---------------------------------------------------------------------------

from agents.chat_tools import (
    analyze_stock as _analyze_stock,
)
from agents.chat_tools import (
    get_market_overview as _get_market_overview,
)
from agents.chat_tools import (
    run_backtest as _run_backtest,
)
from agents.chat_tools import (
    screen_stocks as _screen_stocks,
)
from agents.chat_tools import (
    search_stock_by_name as _search_stock_by_name,
)


@mcp.tool()
def search_stock_by_name(keyword: str) -> list[dict]:
    """根据关键词搜索 A 股股票，支持名称、代码、拼音首字母模糊搜索。"""
    return _search_stock_by_name(keyword=keyword, tool_context=_ctx)


@mcp.tool()
def analyze_stock(
    code: str, mode: Literal["diagnose", "price"] = "diagnose", cost: float = 0.0, days: int = 30
) -> dict:
    """分析单只 A 股。

    **调用时机**：用户问某只股票怎么样、做个诊断、查价格时调用。
    - mode='diagnose'：Wyckoff 结构诊断（阶段、支撑压力、趋势强度、操作建议）
    - mode='price'：返回近 N 天 OHLCV 数据
    **结果处理**：诊断结果较专业，请用通俗语言解释给用户。
    """
    return _analyze_stock(code=code, mode=mode, cost=cost, days=days, tool_context=_ctx)


@mcp.tool()
def get_market_overview() -> dict:
    """获取当前 A 股大盘环境概览。

    **调用时机**：用户问"大盘怎么样"、"今天市场如何"、需要判断整体环境时调用。
    返回上证、深证、创业板指数及涨跌幅。
    """
    return _get_market_overview(tool_context=_ctx)


@mcp.tool()
def screen_stocks(board: Literal["all", "main_chinext"] = "all") -> dict:
    """运行 Wyckoff 五层漏斗筛选，从全市场筛选结构性机会股票。

    **调用时机**：用户说"帮我选股"、"今天有什么机会"、"跑一下漏斗"时调用。
    **注意**：耗时 2-3 分钟，请提前告知用户需要等待。
    **结果处理**：返回候选股票列表和分数，请用专业但易懂的方式呈现。
    """
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
    """回测威科夫五层漏斗策略的历史表现。

    **调用时机**：用户说"回测一下"、"看看历史表现"时调用。
    **注意**：耗时 3-10 分钟，请提前告知用户。
    **结果处理**：返回胜率、收益率、最大回撤等指标，请对比基准解读。
    """
    return _run_backtest(
        start=start,
        end=end,
        hold_days=hold_days,
        top_n=top_n,
        board=board,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        tool_context=_ctx,
    )


# ---------------------------------------------------------------------------
# Tier 2+: 引擎直连工具（无需 LLM，返回纯结构数据）
# ---------------------------------------------------------------------------


@mcp.tool()
def market_regime() -> dict:
    """获取 A 股市场水温和动态阈值。纯引擎计算，不经过 LLM。

    **调用时机**：需要量化判断当前是牛市还是熊市、是否适合加仓时调用。
    返回 regime 枚举（RISK_ON/NEUTRAL/RISK_OFF/CRASH/PANIC_REPAIR）及斜率、3日收益等指标。
    **结果处理**：regime 是核心字段，请据此给出仓位建议。
    """
    from datetime import date as _date
    from datetime import timedelta

    from core.wyckoff_engine import FunnelConfig
    from integrations.data_source import fetch_index_hist
    from tools.market_regime import analyze_benchmark_and_tune_cfg

    end = _date.today()
    start = end - timedelta(days=400)
    bench_df = fetch_index_hist("000001", start, end)
    smallcap_df = fetch_index_hist("399006", start, end)
    return analyze_benchmark_and_tune_cfg(bench_df, smallcap_df, FunnelConfig(), breadth=None)


@mcp.tool()
def wyckoff_diagnose(code: str) -> dict:
    """单股 Wyckoff 结构诊断。纯引擎计算，不经过 LLM，返回结构化数据。

    **调用时机**：需要精确的 Wyckoff 阶段判定和触发信号检测时调用（比 analyze_stock 更底层）。
    返回交易区间(TR)、触发信号(Spring/SOS/LPS/EVR)、阶段和事件分类。
    **结果处理**：trading_range 和 triggers 是核心，请结合阶段判断当前是吸筹/派发/标记。
    """
    import dataclasses
    from datetime import date as _date
    from datetime import timedelta

    from core.stock_cache import normalize_hist_df
    from core.wyckoff_engine import FunnelConfig
    from core.wyckoff_events import classify_wyckoff_event
    from core.wyckoff_v2_structure import detect_structure_triggers, identify_trading_range
    from integrations.stock_hist_repository import get_stock_hist

    end = _date.today()
    start = end - timedelta(days=500)
    raw = get_stock_hist(code, start, end)
    if raw is None or raw.empty:
        return {"error": f"无法获取 {code} 的行情数据"}

    df = normalize_hist_df(raw)
    cfg = FunnelConfig()
    tr = identify_trading_range(df, cfg)
    result = detect_structure_triggers([code], {code: df}, cfg)

    stock_triggers = []
    for trig_type in ("spring", "sos", "lps", "evr"):
        for sym, _score in result.triggers.get(trig_type, []):
            if sym == code:
                stock_triggers.append(trig_type)

    stage = result.stage_map.get(code, "")
    event = classify_wyckoff_event(stock_triggers, stage=stage)

    return {
        "code": code,
        "trading_range": dataclasses.asdict(tr) if tr else None,
        "triggers": stock_triggers,
        "stage": stage,
        "event": dataclasses.asdict(event),
    }


@mcp.tool()
def run_funnel_simulation(board: Literal["all", "main_chinext"] = "all") -> dict:
    """运行 Wyckoff 五层漏斗仿真，返回原始结构数据。

    **调用时机**：用户说"今天有什么机会"、"帮我复盘并推荐"时调用。与 screen_stocks 类似但返回更底层的原始数据。
    **注意**：耗时 30-60 秒，请耐心等待。
    **结果处理**：candidates 是最终候选列表，details 含每层的筛选计数和触发信号明细。
    请用专业研报格式输出，不要直接扔原始 JSON。
    """
    import os

    board_name = str(board or "all").strip().lower()
    if board_name == "main_chinext":
        board_name = "all"
    if board_name not in {"all", "main", "chinext"}:
        return {"error": f"不支持的 board 值 '{board}'，可选: all / main_chinext"}

    prev_mode = os.environ.get("FUNNEL_POOL_MODE")
    prev_board = os.environ.get("FUNNEL_POOL_BOARD")
    prev_exec = os.environ.get("FUNNEL_EXECUTOR_MODE")
    os.environ["FUNNEL_POOL_MODE"] = "board"
    os.environ["FUNNEL_POOL_BOARD"] = board_name
    os.environ["FUNNEL_EXECUTOR_MODE"] = "thread"

    from scripts.wyckoff_funnel import run as run_funnel

    try:
        ok, symbols, bench_ctx, details = run_funnel("", notify=False, return_details=True)
    finally:
        if prev_mode is None:
            os.environ.pop("FUNNEL_POOL_MODE", None)
        else:
            os.environ["FUNNEL_POOL_MODE"] = prev_mode
        if prev_board is None:
            os.environ.pop("FUNNEL_POOL_BOARD", None)
        else:
            os.environ["FUNNEL_POOL_BOARD"] = prev_board
        if prev_exec is None:
            os.environ.pop("FUNNEL_EXECUTOR_MODE", None)
        else:
            os.environ["FUNNEL_EXECUTOR_MODE"] = prev_exec

    if not ok:
        return {"error": "漏斗运行失败", "details": details}
    return {
        "success": True,
        "candidates": symbols,
        "regime": bench_ctx,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Tier 3: 需 Supabase 用户认证
# ---------------------------------------------------------------------------

from agents.chat_tools import (
    generate_ai_report as _generate_ai_report,
)
from agents.chat_tools import (
    generate_strategy_decision as _generate_strategy_decision,
)
from agents.chat_tools import (
    portfolio as _portfolio,
)
from agents.chat_tools import (
    update_portfolio as _update_portfolio,
)


@mcp.tool()
def portfolio(mode: Literal["view", "diagnose"] = "view") -> dict:
    """查看或诊断用户持仓。

    **调用时机**：用户问"我的持仓"、"帮我体检一下"时调用。
    - mode='view'：仅列出持仓明细和盈亏
    - mode='diagnose'：对每只持仓股做 Wyckoff 结构诊断
    """
    return _portfolio(mode=mode, tool_context=_ctx)


@mcp.tool()
def update_portfolio(
    action: Literal["add", "remove", "update", "set_cash", "delete_records"],
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    table: str = "",
    codes: list[str] | None = None,
) -> dict:
    """管理用户持仓或删除追踪记录。

    **调用时机**：用户说"买入/卖出/调仓"、"设置现金"、"删除记录"时调用。
    **危险操作**：会修改用户数据，请确认用户意图后再调用。
    """
    return _update_portfolio(
        action=action,
        code=code,
        name=name,
        shares=shares,
        cost_price=cost_price,
        buy_dt=buy_dt,
        free_cash=free_cash,
        table=table,
        codes=codes,
        tool_context=_ctx,
    )


@mcp.tool()
def generate_ai_report(stock_codes: list[str]) -> dict:
    """对指定股票列表生成威科夫三阵营 AI 深度研报。

    **调用时机**：用户说"出个研报"、"深度分析这几只"时调用。
    **注意**：耗时约 1 分钟，需要 LLM API 配额。
    **结果处理**：返回三阵营（进攻/防守/观察）分类和详细分析，可直接呈现。
    """
    return _generate_ai_report(stock_codes=stock_codes, tool_context=_ctx)


@mcp.tool()
def generate_strategy_decision() -> dict:
    """生成持仓去留决策和新标的买入策略。

    **调用时机**：用户说"该怎么操作"、"给个策略"、"持仓怎么调"时调用。
    **注意**：需要 LLM API 配额。
    **结果处理**：返回每只持仓的操作建议（持有/减仓/清仓）和新标的买入建议。
    """
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
