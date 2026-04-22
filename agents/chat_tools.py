# -*- coding: utf-8 -*-
"""
ADK 工具函数 — 将已有的 Wyckoff 引擎能力暴露给对话 Agent。

每个函数都是一个 ADK tool：普通 Python 函数 + 类型标注 + docstring，
ADK 的 FunctionTool 会自动解析为工具 schema。

用户凭据（API Key / Tushare Token 等）按需从 Supabase 实时获取，
不依赖 st.session_state 或 os.environ 的长链传递。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

try:
    from google.adk.tools import ToolContext
except ImportError:
    # CLI 模式下无 ADK，使用 shim
    class ToolContext:  # type: ignore[no-redef]
        def __init__(self, state=None):
            self.state = state or {}

logger = logging.getLogger(__name__)

_NAME_MAP: dict[str, str] | None = None


def _code_to_name(code: str) -> str:
    """根据股票代码查名称，基于 get_all_stocks() 缓存。"""
    global _NAME_MAP
    if _NAME_MAP is None:
        try:
            from integrations.fetch_a_share_csv import get_all_stocks
            _NAME_MAP = {s["code"]: s["name"] for s in get_all_stocks()}
        except Exception:
            _NAME_MAP = {}
    return _NAME_MAP.get(code, code)


# ---------------------------------------------------------------------------
# 用户凭据：从 Supabase 实时获取 + 进程内短期缓存
# ---------------------------------------------------------------------------

_cred_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cred_cache_lock = threading.Lock()
_CRED_CACHE_TTL = 300  # 5 分钟


def _load_user_credentials(user_id: str) -> dict[str, Any]:
    """
    从 Supabase user_settings 表获取用户凭据，带 5 分钟内存缓存。

    返回原始 row dict（可能为空 dict）。
    """
    if not user_id:
        return {}

    now = time.monotonic()
    with _cred_cache_lock:
        cached = _cred_cache.get(user_id)
        if cached and (now - cached[0]) < _CRED_CACHE_TTL:
            return cached[1]

    try:
        from integrations.supabase_portfolio import load_user_settings_admin
        row = load_user_settings_admin(user_id) or {}
    except Exception as e:
        logger.warning("_load_user_credentials failed for %s: %s", user_id, e)
        row = {}

    with _cred_cache_lock:
        _cred_cache[user_id] = (time.monotonic(), row)

    return row


def _get_user_id(tool_context: ToolContext | None = None) -> str:
    """从 ADK tool_context 的 session state 获取 user_id。"""
    if tool_context is not None:
        uid = tool_context.state.get("user_id", "")
        if uid:
            return str(uid)
    return ""


def _get_credential(tool_context: ToolContext | None, key: str, env_fallback: str = "") -> str:
    """
    获取用户凭据：Supabase → wyckoff.json → 环境变量 → 空串。

    Args:
        tool_context: ADK 注入的上下文（含 user_id）
        key: user_settings 表中的列名，如 "gemini_api_key" / "tushare_token"
        env_fallback: 可选的环境变量名作为兜底
    """
    user_id = _get_user_id(tool_context)
    if user_id:
        creds = _load_user_credentials(user_id)
        val = str(creds.get(key, "") or "").strip()
        if val:
            return val
    # 第二层：wyckoff.json 本地配置
    try:
        from cli.auth import load_config
        local_val = str(load_config().get(key, "") or "").strip()
        if local_val:
            return local_val
    except Exception:
        pass
    # 兜底：环境变量（适用于本地开发 / 未登录场景）
    if env_fallback:
        return os.getenv(env_fallback, "").strip()
    return ""


def _ensure_tushare_token(tool_context: ToolContext | None) -> None:
    """确保 tushare 能拿到 token：从 Supabase 获取后设置到环境变量。

    tushare 库内部只认 ts.set_token() 或 TUSHARE_TOKEN 环境变量，
    无法通过函数参数传递，所以这里做一次即时注入。
    """
    token = _get_credential(tool_context, "tushare_token", "TUSHARE_TOKEN")
    if token:
        os.environ["TUSHARE_TOKEN"] = token


# ---------------------------------------------------------------------------
# Tool 1: 股票搜索
# ---------------------------------------------------------------------------

def search_stock_by_name(keyword: str, tool_context: ToolContext) -> list[dict]:
    """根据关键词搜索 A 股股票，支持名称和代码双向模糊搜索。

    Args:
        keyword: 搜索关键词，如 "宁德" 或 "300750" 或 "600519"

    Returns:
        匹配的股票列表，每项包含 code、name 字段。最多返回 10 条。
    """
    try:
        from integrations.fetch_a_share_csv import get_all_stocks

        stocks = get_all_stocks()
        if not stocks:
            return [{"error": "无法获取股票列表"}]

        kw = keyword.strip()
        results = []
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            if kw in name or kw in code:
                results.append({"code": code, "name": name})
                if len(results) >= 10:
                    break

        return results if results else [{"message": f"未找到与 '{kw}' 匹配的股票"}]
    except Exception as e:
        logger.exception("search_stock_by_name error")
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# Tool 2: 个股诊断
# ---------------------------------------------------------------------------

def diagnose_stock(code: str, cost: float = 0.0, tool_context: ToolContext = None) -> dict:
    """对单只 A 股股票做 Wyckoff 结构化健康诊断。

    诊断内容包括：均线结构、L2 通道分类、吸筹阶段、L4 触发信号（SOS/Spring/LPS/EVR）、
    退出信号、止损状态、量能与振幅等。

    Args:
        code: 6 位股票代码，如 "000001" 或 "600519"
        cost: 持仓成本价，默认 0 表示未持仓，仅做技术面诊断

    Returns:
        结构化诊断结果 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        from integrations.stock_hist_repository import get_stock_hist
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text

        # 拉取 320 个交易日数据（Supabase 缓存优先）
        end_date = date.today()
        start_date = end_date - timedelta(days=500)
        df = get_stock_hist(code, start_date, end_date)

        if df is None or df.empty:
            return {"error": f"无法获取 {code} 的行情数据"}

        from core.stock_cache import _COL_MAP
        df = df.rename(columns=_COL_MAP)

        name = _code_to_name(code)

        d = diagnose_one_stock(code, name, cost, df)
        text = format_diagnostic_text(d)
        return {
            "code": d.code,
            "name": d.name,
            "health": d.health,
            "pnl_pct": round(d.pnl_pct, 2),
            "latest_close": d.latest_close,
            "ma_pattern": d.ma_pattern,
            "l2_channel": d.l2_channel,
            "track": d.track,
            "accum_stage": d.accum_stage,
            "l4_triggers": d.l4_triggers,
            "exit_signal": d.exit_signal,
            "stop_loss_status": d.stop_loss_status,
            "vol_ratio_20_60": round(d.vol_ratio_20_60, 2),
            "range_60d_pct": round(d.range_60d_pct, 1),
            "ret_10d_pct": round(d.ret_10d_pct, 1),
            "ret_20d_pct": round(d.ret_20d_pct, 1),
            "from_year_high_pct": round(d.from_year_high_pct, 1),
            "from_year_low_pct": round(d.from_year_low_pct, 1),
            "health_reasons": d.health_reasons,
            "formatted_text": text,
        }
    except Exception as e:
        logger.exception("diagnose_stock error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: 持仓诊断
# ---------------------------------------------------------------------------

def diagnose_portfolio(tool_context: ToolContext) -> dict:
    """诊断当前用户所有持仓的健康状况。

    从 Supabase 加载用户持仓，对每只股票运行 Wyckoff 健康诊断。

    Returns:
        包含所有持仓诊断结果的 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        from integrations.stock_hist_repository import get_stock_hist
        from integrations.supabase_portfolio import (
            load_portfolio_state,
            build_user_live_portfolio_id,
        )
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text

        user_id = _get_user_id(tool_context)
        if not user_id:
            return {"error": "未登录，请先执行 /login"}

        portfolio_id = build_user_live_portfolio_id(user_id)
        logger.info("diagnose_portfolio: user_id=%s, portfolio_id=%s", user_id, portfolio_id)

        _user_client = _get_user_client(tool_context)
        state = load_portfolio_state(portfolio_id, client=_user_client)
        if state is None:
            from integrations.supabase_portfolio import is_supabase_configured
            if not is_supabase_configured():
                return {"error": "Supabase 未配置（缺少 SUPABASE_URL/KEY 环境变量）"}
            return {"error": f"未找到持仓记录 (portfolio_id={portfolio_id})，请确认 Web 端已录入持仓"}
        if not state.get("positions"):
            return {
                "message": "持仓记录存在但无头寸",
                "portfolio_id": portfolio_id,
                "free_cash": state.get("free_cash", 0),
                "positions": [],
            }

        end_date = date.today()
        start_date = end_date - timedelta(days=500)
        results = []
        for pos in state["positions"]:
            code = pos["code"]
            name = pos.get("name", code)
            cost = float(pos.get("cost", 0))
            try:
                df = get_stock_hist(code, start_date, end_date)
                if df is None or df.empty:
                    results.append({"code": code, "name": name, "error": "无行情数据"})
                    continue
                from core.stock_cache import _COL_MAP
                df = df.rename(columns=_COL_MAP)
                d = diagnose_one_stock(code, name, cost, df)
                results.append({
                    "code": d.code,
                    "name": d.name,
                    "health": d.health,
                    "pnl_pct": round(d.pnl_pct, 2),
                    "latest_close": d.latest_close,
                    "l2_channel": d.l2_channel,
                    "l4_triggers": d.l4_triggers,
                    "health_reasons": d.health_reasons,
                    "formatted_text": format_diagnostic_text(d),
                })
            except Exception as e:
                results.append({"code": code, "name": name, "error": str(e)})

        # ── 盘中信号增强（需要 TickFlow） ──
        intraday_signals: list[dict] = []
        tickflow_hint = ""
        tickflow_key = _get_credential(tool_context, "tickflow_api_key", "TICKFLOW_API_KEY")
        if tickflow_key:
            try:
                from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol
                from core.intraday_sell_signals import PositionSnapshot, scan_position

                tf = TickFlowClient(api_key=tickflow_key)
                codes = [p["code"] for p in state["positions"]]
                quotes = tf.get_quotes(codes)
                for pos in state["positions"]:
                    sym = normalize_cn_symbol(pos["code"])
                    q = quotes.get(sym)
                    if not q:
                        continue
                    snap = PositionSnapshot(
                        code=pos["code"],
                        name=pos.get("name", pos["code"]),
                        cost=float(pos.get("cost", 0)),
                        shares=int(pos.get("shares", 0)),
                        stop_loss=pos.get("stop_loss"),
                        current_price=float(q.get("close", 0) or q.get("last", 0) or 0),
                        open_price=float(q.get("open", 0) or 0),
                        prev_close=float(q.get("prev_close", 0) or q.get("preClose", 0) or 0),
                    )
                    # 获取分钟K线
                    df_1m = df_5m = None
                    yday_vol = 0.0
                    try:
                        df_d = tf.get_klines(sym, period="1d", count=2, intraday=False)
                        if df_d is not None and len(df_d) >= 2:
                            yday_vol = float(df_d.iloc[-2]["volume"])
                    except Exception:
                        pass
                    try:
                        df_1m = tf.get_intraday(sym, period="1m", count=500)
                    except Exception:
                        pass
                    try:
                        df_5m = tf.get_intraday(sym, period="5m", count=500)
                    except Exception:
                        pass
                    from datetime import datetime as _dt
                    from zoneinfo import ZoneInfo as _ZI
                    check_gap = _dt.now(_ZI("Asia/Shanghai")).hour < 12
                    sigs = scan_position(snap, df_1m, df_5m, yday_vol, check_gap=check_gap)
                    for s in sigs:
                        intraday_signals.append({
                            "code": s.code, "name": s.name,
                            "signal_type": s.signal_type,
                            "severity": s.severity,
                            "detail": s.detail,
                        })
            except Exception as e:
                logger.warning("盘中信号增强失败: %s", e)
        else:
            tickflow_hint = "盘中实时信号（止损穿破/跳空低开/放量滞涨/VWAP破位）需要 TickFlow 数据源，购买链接: https://tickflow.org/auth/register?ref=5N4NKTCPL4"

        result = {
            "portfolio_id": portfolio_id,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state["positions"]),
            "diagnostics": results,
        }
        if intraday_signals:
            result["intraday_signals"] = intraday_signals
        if tickflow_hint:
            result["tickflow_hint"] = tickflow_hint
        return result
    except Exception as e:
        logger.exception("diagnose_portfolio error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 4: 行情查询
# ---------------------------------------------------------------------------

def get_stock_price(code: str, days: int = 30, tool_context: ToolContext = None) -> dict:
    """获取指定股票的近期行情数据（OHLCV）。

    Args:
        code: 6 位股票代码，如 "000001"
        days: 获取天数，默认 30，最大 250

    Returns:
        包含最近 N 天 OHLCV 数据的 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        from integrations.stock_hist_repository import get_stock_hist

        days = min(max(days, 1), 250)
        end_date = date.today()
        start_date = end_date - timedelta(days=int(days * 1.6))
        df = get_stock_hist(code, start_date, end_date)

        if df is None or df.empty:
            return {"error": f"无法获取 {code} 的行情数据"}

        from core.stock_cache import _COL_MAP
        df = df.rename(columns=_COL_MAP)
        df = df.tail(days)

        latest = df.iloc[-1] if len(df) > 0 else {}
        records = []
        for _, row in df.iterrows():
            records.append({
                "date": str(row.get("date", "")),
                "open": round(float(row.get("open", 0)), 2),
                "high": round(float(row.get("high", 0)), 2),
                "low": round(float(row.get("low", 0)), 2),
                "close": round(float(row.get("close", 0)), 2),
                "volume": int(row.get("volume", 0)),
                "pct_chg": round(float(row.get("pct_chg", 0)), 2),
            })

        return {
            "code": code,
            "days": len(records),
            "latest_close": round(float(latest.get("close", 0)), 2),
            "latest_date": str(latest.get("date", "")),
            "data": records,
        }
    except Exception as e:
        logger.exception("get_stock_price error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 5: 大盘概览
# ---------------------------------------------------------------------------

def get_market_overview(tool_context: ToolContext) -> dict:
    """获取当前 A 股大盘环境概览。

    返回主要指数（上证、深证、创业板）的最新收盘数据和涨跌幅。

    Returns:
        大盘概览 dict，包含各指数的涨跌幅和近期走势。
    """
    try:
        errors: list[str] = []
        indices = {
            "000001.SH": "上证指数",
            "399001.SZ": "深证成指",
            "399006.SZ": "创业板指",
            "000016.SH": "上证50",
            "000905.SH": "中证500",
        }

        # 优先 tushare（有 token 时数据更稳定）
        try:
            _ensure_tushare_token(tool_context)
            from integrations.tushare_client import get_pro

            pro = get_pro()
            if pro is not None:
                end_date = date.today().strftime("%Y%m%d")
                start_date = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
                result = {}
                for ts_code, name in indices.items():
                    try:
                        df = pro.index_daily(
                            ts_code=ts_code,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        if df is not None and not df.empty:
                            df = df.sort_values("trade_date")
                            latest = df.iloc[-1]
                            result[name] = {
                                "ts_code": ts_code,
                                "trade_date": str(latest.get("trade_date", "")),
                                "close": round(float(latest.get("close", 0)), 2),
                                "pct_chg": round(float(latest.get("pct_chg", 0)), 2),
                                "vol": int(latest.get("vol", 0)),
                                "amount": round(float(latest.get("amount", 0)), 2),
                            }
                    except Exception as e:
                        result[name] = {"error": str(e)}
                if result:
                    return {"indices": result, "source": "tushare"}
            else:
                errors.append("tushare: token 未配置或 client 不可用")
        except Exception as e:
            errors.append(f"tushare: {e}")

        # 兜底 akshare（无需 token）
        try:
            import akshare as ak

            spot = ak.stock_zh_index_spot_em()
            if spot is None or spot.empty:
                errors.append("akshare: stock_zh_index_spot_em 返回空")
            else:
                # 兼容不同版本列名
                col_code = "代码" if "代码" in spot.columns else ("指数代码" if "指数代码" in spot.columns else "")
                col_name = "名称" if "名称" in spot.columns else ("指数名称" if "指数名称" in spot.columns else "")
                col_close = "最新价" if "最新价" in spot.columns else ("最新" if "最新" in spot.columns else "")
                col_pct = "涨跌幅" if "涨跌幅" in spot.columns else ("涨跌幅(%)" if "涨跌幅(%)" in spot.columns else "")
                col_vol = "成交量" if "成交量" in spot.columns else ""
                col_amount = "成交额" if "成交额" in spot.columns else ""
                if not col_code:
                    errors.append("akshare: 缺少指数代码列")
                else:
                    code_to_ts = {
                        "000001": "000001.SH",
                        "399001": "399001.SZ",
                        "399006": "399006.SZ",
                        "000016": "000016.SH",
                        "000905": "000905.SH",
                    }
                    target_codes = set(code_to_ts.keys())
                    today = date.today().strftime("%Y%m%d")
                    result = {}
                    for _, row in spot.iterrows():
                        code_raw = str(row.get(col_code, "") or "").strip()
                        code = "".join(ch for ch in code_raw if ch.isdigit())[-6:]
                        if code not in target_codes:
                            continue
                        name_cn = str(row.get(col_name, "") or "").strip() or indices[code_to_ts[code]]
                        try:
                            close_v = float(row.get(col_close, 0) or 0) if col_close else 0.0
                        except Exception:
                            close_v = 0.0
                        try:
                            pct_v = float(row.get(col_pct, 0) or 0) if col_pct else 0.0
                        except Exception:
                            pct_v = 0.0
                        try:
                            vol_v = int(float(row.get(col_vol, 0) or 0)) if col_vol else 0
                        except Exception:
                            vol_v = 0
                        try:
                            amount_v = round(float(row.get(col_amount, 0) or 0), 2) if col_amount else 0.0
                        except Exception:
                            amount_v = 0.0

                        result[name_cn] = {
                            "ts_code": code_to_ts[code],
                            "trade_date": today,
                            "close": round(close_v, 2),
                            "pct_chg": round(pct_v, 2),
                            "vol": vol_v,
                            "amount": amount_v,
                        }

                    if result:
                        return {"indices": result, "source": "akshare"}
                    errors.append("akshare: 目标指数未命中")
        except Exception as e:
            errors.append(f"akshare: {e}")

        return {
            "error": "无法获取大盘数据",
            "details": "; ".join(errors) if errors else "unknown",
        }
    except Exception as e:
        logger.exception("get_market_overview error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: Wyckoff 漏斗筛选
# ---------------------------------------------------------------------------

_VALID_BOARDS = {"all", "main", "chinext"}
_BOARD_ALIAS = {"gem": "chinext", "创业板": "chinext", "主板": "main", "全部": "all"}


def screen_stocks(board: str = "all", tool_context: ToolContext = None) -> dict:
    """运行 Wyckoff 五层漏斗筛选，从全市场中筛选出具有结构性机会的股票。

    筛选过程包括：L1 基本面过滤、L2 通道分类、L3 板块轮动、L4 触发检测、L5 退出信号过滤。
    整个过程可能需要几分钟时间。

    Args:
        board: 股票池板块，可选 "all"（全部主板+创业板）、"main"（仅主板）、"chinext"（仅创业板）

    Returns:
        筛选结果 dict，包含各层统计和最终候选股票列表。
    """
    try:
        _ensure_tushare_token(tool_context)
        # 参数校验与别名映射
        board = str(board or "all").strip().lower()
        board = _BOARD_ALIAS.get(board, board)
        if board not in _VALID_BOARDS:
            return {"error": f"不支持的 board 值 '{board}'，可选: all / main / chinext"}

        # 保存并设置环境变量（调用后恢复）
        prev_mode = os.environ.get("FUNNEL_POOL_MODE")
        prev_board = os.environ.get("FUNNEL_POOL_BOARD")
        os.environ["FUNNEL_POOL_MODE"] = "board"
        os.environ["FUNNEL_POOL_BOARD"] = board

        from core.funnel_pipeline import run_funnel

        try:
            ok, symbols, bench_ctx, details = run_funnel(
                "", notify=False, return_details=True,
            )
        finally:
            # 恢复环境变量，避免影响后续调用
            if prev_mode is None:
                os.environ.pop("FUNNEL_POOL_MODE", None)
            else:
                os.environ["FUNNEL_POOL_MODE"] = prev_mode
            if prev_board is None:
                os.environ.pop("FUNNEL_POOL_BOARD", None)
            else:
                os.environ["FUNNEL_POOL_BOARD"] = prev_board

        metrics = details.get("metrics") or {}
        triggers = details.get("triggers") or {}
        name_map = details.get("name_map") or {}

        trigger_summary = {}
        for trigger_name, rows in triggers.items():
            trigger_summary[trigger_name] = [
                {
                    "code": str(code),
                    "name": str(name_map.get(str(code), code)),
                    "score": round(float(score), 2),
                }
                for code, score in rows
            ]

        return {
            "ok": bool(ok),
            "summary": {
                "total_scanned": int(metrics.get("total_symbols", 0)),
                "layer1_passed": int(metrics.get("layer1", 0)),
                "layer2_passed": int(metrics.get("layer2", 0)),
                "layer3_passed": int(metrics.get("layer3", 0)),
            },
            "trigger_groups": trigger_summary,
            "top_sectors": metrics.get("top_sectors", []),
            "symbols_for_report": symbols,
        }
    except Exception as e:
        logger.exception("screen_stocks error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 7: AI 研报生成
# ---------------------------------------------------------------------------

def generate_ai_report(stock_codes: list[str], tool_context: ToolContext) -> dict:
    """对指定股票列表生成威科夫三阵营 AI 深度研报。

    使用 LLM 对股票进行威科夫供需分析，将股票分为三个阵营：
    - 逻辑破产 (Invalidated)
    - 储备营地 (Building Cause)
    - 起跳板 (On the Springboard)

    需要配置 Gemini API Key 才能使用。

    Args:
        stock_codes: 股票代码列表，如 ["000001", "600519", "300750"]，最多 10 只

    Returns:
        包含研报文本和起跳板代码的 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        if not stock_codes:
            return {"error": "请提供至少一个股票代码"}
        if len(stock_codes) > 10:
            stock_codes = stock_codes[:10]

        # 从 Supabase 获取 Gemini 凭据，兜底环境变量
        api_key = _get_credential(tool_context, "gemini_api_key", "GEMINI_API_KEY")
        model = _get_credential(tool_context, "gemini_model", "GEMINI_MODEL") or "gemini-2.0-flash"
        base_url = _get_credential(tool_context, "gemini_base_url", "")
        if not api_key:
            return {"error": "未配置 Gemini API Key，无法生成 AI 研报。请在设置页面配置。"}

        # 构建 symbols_info 格式
        symbols_info = []
        for code in stock_codes:
            code = str(code).strip()
            name = _code_to_name(code)
            symbols_info.append({"code": code, "name": name, "tag": "chat_request"})

        from core.batch_report import run_step3

        ok, reason, report_text = run_step3(
            symbols_info,
            webhook_url="",
            api_key=api_key,
            model=model,
            benchmark_context=None,
            notify=False,
            provider="gemini",
            llm_base_url=base_url,
        )

        return {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "report_text": str(report_text or ""),
            "model": model,
            "stock_count": len(symbols_info),
        }
    except Exception as e:
        logger.exception("generate_ai_report error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 8: 持仓策略决策
# ---------------------------------------------------------------------------

def generate_strategy_decision(tool_context: ToolContext) -> dict:
    """生成持仓去留决策和新标的买入策略（需要先运行筛选和研报）。

    使用威科夫方法论，综合审视当前持仓和外部候选，给出：
    - 现有持仓的去留决策（EXIT/TRIM/HOLD）
    - 外部候选的买入建议（PROBE/ATTACK）

    需要配置 Gemini API Key 和持仓数据。

    Returns:
        策略决策结果 dict。
    """
    try:
        _ensure_tushare_token(tool_context)

        # 从 Supabase 获取 Gemini 凭据
        api_key = _get_credential(tool_context, "gemini_api_key", "GEMINI_API_KEY")
        model = _get_credential(tool_context, "gemini_model", "GEMINI_MODEL") or "gemini-2.0-flash"
        base_url = _get_credential(tool_context, "gemini_base_url", "")
        if not api_key:
            return {"error": "未配置 Gemini API Key，无法生成策略决策。请在设置页面配置。"}

        user_id = _get_user_id(tool_context)
        if not user_id:
            return {"error": "未找到用户 ID，无法加载持仓进行策略分析"}

        from integrations.supabase_portfolio import build_user_live_portfolio_id

        portfolio_id = build_user_live_portfolio_id(user_id)

        # 先运行筛选获取候选
        screen_result = screen_stocks(board="all")
        if screen_result.get("error"):
            return {"error": f"筛选失败: {screen_result['error']}"}

        symbols_info = screen_result.get("symbols_for_report", [])

        # 生成研报
        report_text = ""
        if symbols_info:
            from core.batch_report import run_step3

            ok, reason, report_text = run_step3(
                symbols_info,
                webhook_url="",
                api_key=api_key,
                model=model,
                benchmark_context=None,
                notify=False,
                provider="gemini",
                llm_base_url=base_url,
            )

        # 生成策略决策（需要 Telegram 配置来发送，但在聊天模式下直接返回结果）
        from core.strategy import run_step4

        tg_bot_token = os.getenv("TG_BOT_TOKEN", "")
        tg_chat_id = os.getenv("TG_CHAT_ID", "")

        if not tg_bot_token or not tg_chat_id:
            return {
                "message": "策略分析完成，但未配置 Telegram 无法发送通知。以下是筛选和研报结果。",
                "screen_summary": screen_result.get("summary", {}),
                "report_preview": (report_text[:2000] + "...") if len(report_text) > 2000 else report_text,
            }

        ok, reason = run_step4(
            external_report=report_text,
            benchmark_context=None,
            api_key=api_key,
            model=model,
            portfolio_id=portfolio_id,
            tg_bot_token=tg_bot_token,
            tg_chat_id=tg_chat_id,
        )

        return {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "screen_summary": screen_result.get("summary", {}),
        }
    except Exception as e:
        logger.exception("generate_strategy_decision error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 9: 推荐跟踪查询
# ---------------------------------------------------------------------------

def get_recommendation_tracking(limit: int = 20) -> dict:
    """查询最近的 AI 推荐记录及其跟踪表现。

    返回历史推荐的股票及其后续涨跌幅表现。

    Args:
        limit: 返回记录数，默认 20，最大 50

    Returns:
        推荐跟踪记录列表。
    """
    try:
        from integrations.supabase_recommendation import load_recommendation_tracking

        limit = min(max(limit, 1), 50)
        records = load_recommendation_tracking(limit=limit)

        if not records:
            return {"message": "暂无推荐跟踪记录", "records": []}

        # 简化返回格式
        simplified = [
            {
                "code": str(r.get("code", "")),
                "name": str(r.get("name", "")),
                "recommend_date": str(r.get("recommend_date", "")),
                "recommend_price": r.get("recommend_price"),
                "current_price": r.get("current_price"),
                "pnl_pct": r.get("pnl_pct"),
                "max_pnl_pct": r.get("max_pnl_pct"),
                "camp": str(r.get("camp", "")),
                "status": str(r.get("status", "")),
            }
            for r in records
        ]

        return {
            "total": len(simplified),
            "records": simplified,
        }
    except Exception as e:
        logger.exception("get_recommendation_tracking error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 10: 信号确认池查询
# ---------------------------------------------------------------------------

def get_signal_pending(status: str = "all", limit: int = 30) -> dict:
    """查询信号确认池（signal_pending）中的信号状态。

    信号确认系统：L4 触发信号（SOS/Spring/LPS/EVR）先进入 pending 池，
    经 1-3 天价格行为确认后变为 confirmed（可操作）或 expired（失效）。

    Args:
        status: 筛选状态，可选 "all"、"pending"、"confirmed"、"expired"，默认 "all"
        limit: 返回记录数，默认 30，最大 100

    Returns:
        信号确认池记录列表。
    """
    try:
        from integrations.supabase_base import create_admin_client, is_admin_configured
        from core.constants import TABLE_SIGNAL_PENDING

        if not is_admin_configured():
            return {"error": "Supabase 未配置，无法查询信号确认池"}

        limit = min(max(limit, 1), 100)
        client = create_admin_client()
        query = client.table(TABLE_SIGNAL_PENDING).select("*")

        if status in ("pending", "confirmed", "expired"):
            query = query.eq("status", status)

        rows = query.order("updated_at", desc=True).limit(limit).execute().data or []

        if not rows:
            status_label = {"pending": "待确认", "confirmed": "已确认", "expired": "已过期"}.get(status, "")
            return {"message": f"暂无{status_label}信号记录", "records": []}

        records = [
            {
                "code": f"{int(r.get('code', 0)):06d}",
                "name": str(r.get("name", "")),
                "signal_type": str(r.get("signal_type", "")),
                "signal_date": str(r.get("signal_date", "")),
                "status": str(r.get("status", "")),
                "days_elapsed": r.get("days_elapsed", 0),
                "ttl_days": r.get("ttl_days", 3),
                "signal_score": r.get("signal_score", 0),
                "snap_close": r.get("snap_close"),
                "confirm_date": str(r.get("confirm_date", "") or ""),
                "expire_date": str(r.get("expire_date", "") or ""),
                "confirm_reason": str(r.get("confirm_reason", "") or ""),
                "regime": str(r.get("regime", "") or ""),
                "industry": str(r.get("industry", "") or ""),
            }
            for r in rows
        ]

        # 统计摘要
        status_counts = {}
        for rec in records:
            s = rec["status"]
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "total": len(records),
            "status_counts": status_counts,
            "records": records,
        }
    except Exception as e:
        logger.exception("get_signal_pending error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helper — 缓存 user client，避免重复消费 refresh_token
# ---------------------------------------------------------------------------

_user_client_cache: dict[str, Any] = {}  # user_id → Client


def _get_user_client(tool_context: ToolContext | None):
    """获取或复用 user client。token 变化时重建 client。"""
    if tool_context is None:
        return None
    at = (tool_context.state.get("access_token") or "")
    if not at:
        return None
    user_id = _get_user_id(tool_context)
    # token 变化（如重新登录）时需重建 client
    cache_key = f"{user_id}:{at[:16]}"
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    rt = (tool_context.state.get("refresh_token") or "")
    from integrations.supabase_base import create_user_client, get_session_tokens
    client = create_user_client(at, rt)
    # 回写刷新后的 token
    new_at, new_rt = get_session_tokens(client)
    if new_at:
        tool_context.state["access_token"] = new_at
    if new_rt:
        tool_context.state["refresh_token"] = new_rt
    _user_client_cache[cache_key] = client
    return client


def _to_ts_code(code: str) -> str:
    """6 位代码 → tushare ts_code（如 000001 → 000001.SZ）。"""
    code = str(code).strip()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


# ---------------------------------------------------------------------------
# Tool 11: 查看持仓（原子数据）
# ---------------------------------------------------------------------------

def get_portfolio(tool_context: ToolContext) -> dict:
    """查看用户当前持仓列表和可用资金。仅返回原始数据，不做诊断分析。

    Returns:
        持仓列表和可用资金。
    """
    try:
        from integrations.supabase_portfolio import (
            build_user_live_portfolio_id,
            load_portfolio_state,
        )

        user_id = _get_user_id(tool_context)
        if not user_id:
            return {"error": "未登录，请先执行 /login"}

        _client = _get_user_client(tool_context)
        portfolio_id = build_user_live_portfolio_id(user_id)
        state = load_portfolio_state(portfolio_id, client=_client)
        if state is None:
            return {"message": "未找到持仓记录", "positions": [], "free_cash": 0}

        positions = []
        for p in state.get("positions", []):
            positions.append({
                "code": p["code"],
                "name": p.get("name", ""),
                "shares": p.get("shares", 0),
                "cost_price": p.get("cost", 0),
                "buy_dt": p.get("buy_dt", ""),
            })

        return {
            "portfolio_id": portfolio_id,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(positions),
            "positions": positions,
        }
    except Exception as e:
        logger.exception("get_portfolio error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 12: 持仓管理
# ---------------------------------------------------------------------------

def update_portfolio(
    action: str,
    code: str = "",
    name: str = "",
    shares: int = 0,
    cost_price: float = 0,
    buy_dt: str = "",
    free_cash: float = 0,
    tool_context: ToolContext = None,
) -> dict:
    """管理用户持仓：新增、修改、删除持仓，或设置可用资金。

    Args:
        action: 操作类型，"add"（新增/加仓）、"update"（修改）、"remove"（删除）、"set_cash"（设置可用资金）
        code: 6 位股票代码（add/update/remove 时必填）
        name: 股票名称（可选）
        shares: 持仓股数
        cost_price: 成本价
        buy_dt: 买入日期（YYYYMMDD 格式）
        free_cash: 可用资金（set_cash 时使用）

    Returns:
        操作结果和最新持仓摘要。
    """
    try:
        from integrations.supabase_portfolio import (
            build_user_live_portfolio_id,
            load_portfolio_state,
            upsert_position,
            delete_position,
            update_free_cash,
        )
        user_id = _get_user_id(tool_context)
        if not user_id:
            return {"error": "未登录，请先执行 /login"}

        client = _get_user_client(tool_context)
        if client is None:
            return {"error": "缺少 access_token，请重新登录"}

        portfolio_id = build_user_live_portfolio_id(user_id)
        action = action.strip().lower()

        if action in ("add", "update"):
            if not code:
                return {"error": "add/update 操作需要提供股票代码 code"}
            code = code.strip()
            # 校验 code-name 匹配
            real_name = _code_to_name(code)
            if real_name and name and real_name != name:
                return {"error": f"代码 {code} 对应的股票是「{real_name}」，而非「{name}」，请确认代码或名称是否正确"}
            if real_name and not name:
                name = real_name
            if not real_name and not name:
                return {"error": f"代码 {code} 在股票列表中未找到，请确认代码是否正确"}
            ok, msg = upsert_position(portfolio_id, {
                "code": code,
                "name": name,
                "shares": shares,
                "cost_price": cost_price,
                "buy_dt": buy_dt,
            }, client=client)
            if not ok:
                return {"error": msg}

        elif action == "remove":
            if not code:
                return {"error": "remove 操作需要提供股票代码 code"}
            ok, msg = delete_position(portfolio_id, code.strip(), client=client)
            if not ok:
                return {"error": msg}

        elif action == "set_cash":
            ok, msg = update_free_cash(portfolio_id, free_cash, client=client)
            if not ok:
                return {"error": msg}

        else:
            return {"error": f"未知操作: {action}，支持 add/update/remove/set_cash"}

        # 返回最新持仓状态
        state = load_portfolio_state(portfolio_id, client=client)
        if not state:
            return {"success": True, "message": msg, "positions": []}
        summary = []
        for p in state.get("positions", []):
            summary.append(f"{p['code']} {p.get('name','')} {p.get('shares',0)}股 成本{p.get('cost',0)}")
        return {
            "success": True,
            "message": msg,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state.get("positions", [])),
            "positions_summary": summary,
        }
    except Exception as e:
        logger.exception("update_portfolio error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 工具列表导出
# ---------------------------------------------------------------------------

WYCKOFF_TOOLS = [
    search_stock_by_name,
    diagnose_stock,
    diagnose_portfolio,
    get_portfolio,
    get_stock_price,
    get_market_overview,
    screen_stocks,
    generate_ai_report,
    generate_strategy_decision,
    get_recommendation_tracking,
    get_signal_pending,
    update_portfolio,
]
