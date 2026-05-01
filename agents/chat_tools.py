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


def _collect_tickflow_limit_hints_from_df(df: Any) -> list[str]:
    if df is None or not hasattr(df, "attrs"):
        return []
    attrs = getattr(df, "attrs", {}) or {}
    hints = attrs.get("tickflow_limit_hints")
    if isinstance(hints, list):
        out: list[str] = []
        for item in hints:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        if out:
            return out
    one = str(attrs.get("tickflow_limit_hint", "") or "").strip()
    return [one] if one else []


def _hist_metadata(df: Any) -> dict[str, Any]:
    """Return compact provenance metadata for model-facing tool results."""
    if df is None or not hasattr(df, "attrs"):
        return {}
    attrs = getattr(df, "attrs", {}) or {}
    meta: dict[str, Any] = {}
    for key in ("source", "upstream_source", "cache_status", "cached_until"):
        val = str(attrs.get(key, "") or "").strip()
        if val:
            meta[key] = val
    upstream_sources = attrs.get("upstream_sources")
    if isinstance(upstream_sources, list):
        clean = [str(x) for x in upstream_sources if str(x or "").strip()]
        if clean:
            meta["upstream_sources"] = clean
    try:
        meta["row_count"] = int(len(df))
    except Exception:
        pass
    return meta


def _latest_hist_date(df: Any, date_col: str = "date") -> str:
    if df is None or not hasattr(df, "empty") or df.empty:
        return ""
    try:
        return str(df.iloc[-1].get(date_col, "") or "")
    except Exception:
        return ""


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


_LOCAL_USER_ID = "local"


def _get_user_id(tool_context: ToolContext | None = None) -> str:
    """从 tool_context 获取 user_id，未登录时降级为本地用户。"""
    if tool_context is not None:
        uid = tool_context.state.get("user_id", "")
        if uid:
            return str(uid)
    return _LOCAL_USER_ID


def _has_cloud(tool_context: ToolContext | None) -> bool:
    """判断是否有 Supabase 云端写入能力。"""
    if not tool_context:
        return False
    return bool(tool_context.state.get("access_token", ""))


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
# Tool 2: 个股分析（合并原 diagnose_stock + get_stock_price）
# ---------------------------------------------------------------------------

def analyze_stock(code: str, mode: str = "diagnose", cost: float = 0.0, days: int = 30, tool_context: ToolContext = None) -> dict:
    """分析单只 A 股股票：Wyckoff 健康诊断或近期行情查询。

    Args:
        code: 6 位股票代码，如 "000001" 或 "600519"
        mode: "diagnose" 做 Wyckoff 结构化健康诊断；"price" 仅返回近期 OHLCV 行情数据
        cost: 持仓成本价，仅 diagnose 模式使用，默认 0 表示未持仓
        days: 获取天数，仅 price 模式使用，默认 30，最大 250

    Returns:
        诊断结果或行情数据 dict。
    """
    try:
        _ensure_tushare_token(tool_context)
        from integrations.stock_hist_repository import get_stock_hist
        from core.stock_cache import _COL_MAP

        mode = (mode or "diagnose").strip().lower()
        if mode not in ("diagnose", "price"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: diagnose, price"}
        end_date = date.today()

        if mode == "price":
            days = min(max(days, 1), 250)
            start_date = end_date - timedelta(days=int(days * 1.6))
            df = get_stock_hist(code, start_date, end_date)
            if df is None or df.empty:
                return {"error": f"无法获取 {code} 的行情数据"}
            hist_hints = _collect_tickflow_limit_hints_from_df(df)
            hist_meta = _hist_metadata(df)
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
                "data_status": "ok",
                **hist_meta,
                "data": records,
                **({"tickflow_limit_hint": hist_hints[0]} if hist_hints else {}),
            }

        # mode == "diagnose"
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text
        start_date = end_date - timedelta(days=500)
        df = get_stock_hist(code, start_date, end_date)
        if df is None or df.empty:
            return {"error": f"无法获取 {code} 的行情数据"}
        hist_hints = _collect_tickflow_limit_hints_from_df(df)
        hist_meta = _hist_metadata(df)
        latest_date = _latest_hist_date(df, "日期")
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
            "data_status": "ok",
            "latest_date": latest_date or _latest_hist_date(df),
            **hist_meta,
            **({"tickflow_limit_hint": hist_hints[0]} if hist_hints else {}),
        }
    except Exception as e:
        logger.exception("analyze_stock error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 3: 持仓（合并原 get_portfolio + diagnose_portfolio）
# ---------------------------------------------------------------------------

def portfolio(mode: str = "view", tool_context: ToolContext = None) -> dict:
    """查看或诊断用户当前持仓。

    Args:
        mode: "view" 仅返回持仓列表和可用资金；"diagnose" 对每只持仓做 Wyckoff 健康诊断

    Returns:
        持仓数据或诊断结果。
    """
    try:
        from integrations.supabase_portfolio import build_user_live_portfolio_id

        user_id = _get_user_id(tool_context)
        portfolio_id = build_user_live_portfolio_id(user_id)
        state = None

        # local-first
        try:
            from integrations.local_db import load_portfolio
            state = load_portfolio(portfolio_id)
        except Exception:
            pass

        if state is None and _has_cloud(tool_context):
            from integrations.supabase_portfolio import load_portfolio_state
            _client = _get_user_client(tool_context)
            state = _with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=_client)
            if state:
                try:
                    from integrations.local_db import save_portfolio
                    save_portfolio(
                        portfolio_id,
                        float(state.get("free_cash", 0) or 0),
                        [
                            {
                                "code": p.get("code", ""),
                                "name": p.get("name", ""),
                                "shares": p.get("shares", 0),
                                "cost_price": p.get("cost", p.get("cost_price", 0)),
                                "stop_loss": p.get("stop_loss"),
                            }
                            for p in state.get("positions", [])
                        ],
                    )
                except Exception:
                    pass

        if state is None:
            return {"message": "未找到持仓记录，可通过 update_portfolio 添加", "positions": [], "free_cash": 0}

        mode = (mode or "view").strip().lower()
        if mode not in ("view", "diagnose"):
            return {"error": f"mode 参数无效: '{mode}'，可选值: view, diagnose"}

        if mode == "view":
            positions = []
            for p in state.get("positions", []):
                positions.append({
                    "code": p.get("code", ""),
                    "name": p.get("name", ""),
                    "shares": p.get("shares", 0),
                    "cost_price": p.get("cost", p.get("cost_price", 0)),
                    "buy_dt": p.get("buy_dt", ""),
                })
            return {
                "portfolio_id": portfolio_id,
                "free_cash": state.get("free_cash", 0),
                "position_count": len(positions),
                "positions": positions,
            }

        # mode == "diagnose"
        _ensure_tushare_token(tool_context)
        from integrations.stock_hist_repository import get_stock_hist
        from core.holding_diagnostic import diagnose_one_stock, format_diagnostic_text

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
        hist_tickflow_hints: list[str] = []
        successful_count = 0
        failed_count = 0
        for pos in state["positions"]:
            pos_code = pos.get("code", "") or pos.get("code", "")
            pos_name = pos.get("name", pos_code)
            pos_cost = float(pos.get("cost", pos.get("cost_price", 0)) or 0)
            try:
                df = get_stock_hist(pos_code, start_date, end_date)
                if df is None or df.empty:
                    failed_count += 1
                    results.append({"code": pos_code, "name": pos_name, "error": "无行情数据"})
                    continue
                hist_meta = _hist_metadata(df)
                latest_date = _latest_hist_date(df, "日期")
                for hint in _collect_tickflow_limit_hints_from_df(df):
                    if hint not in hist_tickflow_hints:
                        hist_tickflow_hints.append(hint)
                from core.stock_cache import _COL_MAP
                df = df.rename(columns=_COL_MAP)
                d = diagnose_one_stock(pos_code, pos_name, pos_cost, df)
                successful_count += 1
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
                    "data_status": "ok",
                    "latest_date": latest_date or _latest_hist_date(df),
                    **hist_meta,
                })
            except Exception as e:
                failed_count += 1
                results.append({"code": pos_code, "name": pos_name, "error": str(e)})

        result = {
            "portfolio_id": portfolio_id,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state["positions"]),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "diagnostics": results,
        }
        if hist_tickflow_hints:
            result["tickflow_limit_hint"] = hist_tickflow_hints[0]
        return result
    except Exception as e:
        logger.exception("portfolio error")
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
        prev_exec = os.environ.get("FUNNEL_EXECUTOR_MODE")
        os.environ["FUNNEL_POOL_MODE"] = "board"
        os.environ["FUNNEL_POOL_BOARD"] = board
        # CLI 后台线程中 fork 子进程会触发 Python 3.13+ fds_to_keep 错误，强制用 thread
        os.environ["FUNNEL_EXECUTOR_MODE"] = "thread"

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
            if prev_exec is None:
                os.environ.pop("FUNNEL_EXECUTOR_MODE", None)
            else:
                os.environ["FUNNEL_EXECUTOR_MODE"] = prev_exec

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
# Tool 5: 历史记录查询（合并原 recommendation / signal / tail_buy）
# ---------------------------------------------------------------------------

def query_history(source: str, status: str = "all", run_date: str = "", decision: str = "", limit: int = 20) -> dict:
    """查询历史记录：AI 推荐追踪、信号确认池或尾盘买入记录。

    Args:
        source: "recommendation" 查推荐追踪；"signal" 查信号确认池；"tail_buy" 查尾盘买入记录
        status: 仅 signal 源使用，"all"/"pending"/"confirmed"/"expired"
        run_date: 仅 tail_buy 源使用，按日期过滤（YYYY-MM-DD）
        decision: 仅 tail_buy 源使用，按决策过滤（BUY/WATCH 等）
        limit: 返回记录数上限，默认 20

    Returns:
        对应来源的历史记录列表。
    """
    source = (source or "").strip().lower()
    if source == "recommendation":
        return _query_recommendation(limit)
    elif source == "signal":
        return _query_signal(status, limit)
    elif source == "tail_buy":
        return _query_tail_buy(run_date, decision, limit)
    else:
        return {"error": f"不支持的 source：{source}，请用 'recommendation'、'signal' 或 'tail_buy'"}


def _query_recommendation(limit: int) -> dict:
    try:
        limit = min(max(limit, 1), 50)
        records = []
        try:
            from integrations.local_db import load_recommendations
            records = load_recommendations(limit=limit)
        except Exception:
            pass
        if not records:
            from integrations.supabase_recommendation import load_recommendation_tracking
            records = load_recommendation_tracking(limit=limit)
            if records:
                try:
                    from integrations.local_db import save_recommendations
                    save_recommendations(records)
                except Exception:
                    pass
        if not records:
            return {"message": "暂无推荐跟踪记录", "records": []}
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
        return {"total": len(simplified), "records": simplified}
    except Exception as e:
        logger.exception("query_history(recommendation) error")
        return {"error": str(e)}


def _query_signal(status: str, limit: int) -> dict:
    try:
        limit = min(max(limit, 1), 100)
        rows: list[dict] = []
        try:
            from integrations.local_db import load_signals
            st = status if status in ("pending", "confirmed", "expired") else None
            rows = load_signals(status=st, limit=limit)
        except Exception:
            pass
        if not rows:
            from integrations.supabase_base import create_admin_client, is_admin_configured
            from core.constants import TABLE_SIGNAL_PENDING
            if not is_admin_configured():
                return {"error": "本地无缓存且 Supabase 未配置"}
            client = create_admin_client()
            query = client.table(TABLE_SIGNAL_PENDING).select("*")
            if status in ("pending", "confirmed", "expired"):
                query = query.eq("status", status)
            rows = query.order("updated_at", desc=True).limit(limit).execute().data or []
            if rows:
                try:
                    from integrations.local_db import save_signals
                    save_signals(rows)
                except Exception:
                    pass
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
        status_counts: dict[str, int] = {}
        for rec in records:
            s = rec["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        return {"total": len(records), "status_counts": status_counts, "records": records}
    except Exception as e:
        logger.exception("query_history(signal) error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helper — 缓存 user client，避免重复消费 refresh_token
# ---------------------------------------------------------------------------

_user_client_cache: dict[str, Any] = {}  # user_id → Client


def _get_user_client(tool_context: ToolContext | None):
    """获取或复用 user client。token 变化时重建；auth 失败自动重登。"""
    if tool_context is None:
        return None
    at = (tool_context.state.get("access_token") or "")
    if not at:
        return None
    user_id = _get_user_id(tool_context)
    cache_key = f"{user_id}:{at[:16]}"
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    rt = (tool_context.state.get("refresh_token") or "")
    from integrations.supabase_base import create_user_client, get_session_tokens
    try:
        client = create_user_client(at, rt)
        new_at, new_rt = get_session_tokens(client)
    except Exception as e:
        if _is_auth_error(e):
            client, new_at, new_rt = _relogin_and_create_client(tool_context)
            if client is None:
                return None
        else:
            raise
    if new_at:
        tool_context.state["access_token"] = new_at
    if new_rt:
        tool_context.state["refresh_token"] = new_rt
    final_key = f"{user_id}:{(new_at or at)[:16]}"
    _user_client_cache[final_key] = client
    return client


def _relogin_and_create_client(tool_context: ToolContext | None):
    """用 wyckoff.json 中的凭证重新登录，返回 (client, access_token, refresh_token)。"""
    from cli.auth import _auto_relogin
    data = _auto_relogin()
    if not data:
        return None, "", ""
    tool_context.state["access_token"] = data["access_token"]
    tool_context.state["refresh_token"] = data["refresh_token"]
    from integrations.supabase_base import create_user_client, get_session_tokens
    client = create_user_client(data["access_token"], data["refresh_token"])
    new_at, new_rt = get_session_tokens(client)
    return client, new_at or data["access_token"], new_rt or data["refresh_token"]


_AUTH_ERR_KEYWORDS = ("invalid", "expired", "revoked", "refresh", "jwt", "token")


def _is_auth_error(e: Exception) -> bool:
    err = str(e).lower()
    return any(k in err for k in _AUTH_ERR_KEYWORDS)


def _with_auth_retry(tool_context: ToolContext | None, fn, *args, **kwargs):
    """执行 fn，遇到 auth 错误时自动重登并重试一次。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not _is_auth_error(e) or tool_context is None:
            raise
    _user_client_cache.clear()
    client, new_at, new_rt = _relogin_and_create_client(tool_context)
    if client is None:
        return None
    if "client" in kwargs:
        kwargs["client"] = client
    elif args:
        args_list = list(args)
        for i, a in enumerate(args_list):
            if hasattr(a, "auth") and hasattr(a, "postgrest"):
                args_list[i] = client
                break
        args = tuple(args_list)
    return fn(*args, **kwargs)


def _to_ts_code(code: str) -> str:
    """6 位代码 → tushare ts_code（如 000001 → 000001.SZ）。"""
    code = str(code).strip()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


# ---------------------------------------------------------------------------
# Tool 6: 持仓管理（合并原 update_portfolio + delete_tracking_records）
# ---------------------------------------------------------------------------

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
    tool_context: ToolContext = None,
) -> dict:
    """管理用户持仓或删除追踪记录。

    Args:
        action: 操作类型，"add"（新增/加仓）、"update"（修改）、"remove"（删除）、"set_cash"（设置可用资金）、"delete_records"（删除推荐/信号记录）
        code: 6 位股票代码（add/update/remove 时必填）
        name: 股票名称（可选）
        shares: 持仓股数
        cost_price: 成本价
        buy_dt: 买入日期（YYYYMMDD 格式）
        free_cash: 可用资金（set_cash 时使用）
        table: 仅 delete_records 使用，'recommendation' 或 'signal'
        codes: 仅 delete_records 使用，要删除的股票代码列表

    Returns:
        操作结果。
    """
    try:
        action = action.strip().lower()

        if action == "delete_records":
            if not codes:
                return {"error": "请指定要删除的股票代码 codes"}
            codes = [str(c).strip() for c in codes if str(c).strip()]
            if table == "recommendation":
                from integrations.local_db import delete_recommendations
                n = delete_recommendations(codes)
                return {"deleted": n, "table": "recommendation_tracking", "codes": codes}
            elif table == "signal":
                from integrations.local_db import delete_signals
                n = delete_signals(codes)
                return {"deleted": n, "table": "signal_pending", "codes": codes}
            else:
                return {"error": f"不支持的表：{table}，请用 'recommendation' 或 'signal'"}

        from integrations.supabase_portfolio import build_user_live_portfolio_id

        user_id = _get_user_id(tool_context)
        portfolio_id = build_user_live_portfolio_id(user_id)
        cloud = _has_cloud(tool_context)
        msg = ""

        if action in ("add", "update"):
            if not code:
                return {"error": "add/update 操作需要提供股票代码 code"}
            code = code.strip()
            real_name = _code_to_name(code)
            if real_name and name and real_name != name:
                return {"error": f"代码 {code} 对应的股票是「{real_name}」，而非「{name}」，请确认代码或名称是否正确"}
            if real_name and not name:
                name = real_name
            if not real_name and not name:
                return {"error": f"代码 {code} 在股票列表中未找到，请确认代码是否正确"}
            if cloud:
                from integrations.supabase_portfolio import upsert_position
                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(tool_context, upsert_position, portfolio_id, {
                    "code": code, "name": name, "shares": shares,
                    "cost_price": cost_price, "buy_dt": buy_dt,
                }, client=client)
                if not ok:
                    return {"error": msg}
            from integrations.local_db import upsert_local_position
            upsert_local_position(portfolio_id, code, name, shares, cost_price, buy_dt)
            msg = msg or f"{code} 已更新"

        elif action == "remove":
            if not code:
                return {"error": "remove 操作需要提供股票代码 code"}
            code = code.strip()
            if cloud:
                from integrations.supabase_portfolio import delete_position
                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(tool_context, delete_position, portfolio_id, code, client=client)
                if not ok:
                    return {"error": msg}
            from integrations.local_db import delete_local_position
            delete_local_position(portfolio_id, code)
            msg = msg or f"{code} 已删除"

        elif action == "set_cash":
            if cloud:
                from integrations.supabase_portfolio import update_free_cash
                client = _get_user_client(tool_context)
                ok, msg = _with_auth_retry(tool_context, update_free_cash, portfolio_id, free_cash, client=client)
                if not ok:
                    return {"error": msg}
            from integrations.local_db import update_local_free_cash
            update_local_free_cash(portfolio_id, free_cash)
            msg = msg or f"可用资金已更新为 {free_cash:,.2f}"

        else:
            return {"error": f"未知操作: {action}，支持 add/update/remove/set_cash/delete_records"}

        # Supabase write-through: 读回最新状态同步到本地
        if cloud:
            try:
                from integrations.supabase_portfolio import load_portfolio_state
                client = _get_user_client(tool_context)
                state = _with_auth_retry(tool_context, load_portfolio_state, portfolio_id, client=client)
                if state:
                    from integrations.local_db import save_portfolio
                    save_portfolio(
                        portfolio_id,
                        float(state.get("free_cash", 0) or 0),
                        [
                            {
                                "code": p.get("code", ""),
                                "name": p.get("name", ""),
                                "shares": p.get("shares", 0),
                                "cost_price": p.get("cost", p.get("cost_price", 0)),
                                "stop_loss": p.get("stop_loss"),
                            }
                            for p in state.get("positions", [])
                        ],
                    )
            except Exception:
                pass

        # 读本地最新状态返回
        from integrations.local_db import load_portfolio
        state = load_portfolio(portfolio_id)
        if not state:
            return {"success": True, "message": msg, "positions": []}

        summary = []
        for p in state.get("positions", []):
            summary.append(f"{p['code']} {p.get('name','')} {p.get('shares',0)}股 成本{p.get('cost_price',0)}")
        result = {
            "success": True,
            "message": msg,
            "free_cash": state.get("free_cash", 0),
            "position_count": len(state.get("positions", [])),
            "positions_summary": summary,
        }
        if not cloud:
            result["storage"] = "local"
        return result
    except Exception as e:
        logger.exception("update_portfolio error")
        return {"error": str(e)}


def _query_tail_buy(run_date: str, decision: str, limit: int) -> dict:
    try:
        limit = min(max(int(limit), 1), 200)
        from integrations.local_db import load_tail_buy_history
        records = load_tail_buy_history(
            run_date=str(run_date or "").strip(),
            decision=str(decision or "").strip(),
            limit=limit,
        )
        if not records:
            from integrations.supabase_tail_buy import load_tail_buy_from_supabase
            sb_rows = load_tail_buy_from_supabase(limit=limit)
            if sb_rows:
                from integrations.local_db import save_tail_buy_results
                save_tail_buy_results([
                    {
                        "code": str(r.get("code", "")),
                        "name": r.get("name", ""),
                        "run_date": str(r.get("run_date", "")),
                        "signal_date": r.get("signal_date", ""),
                        "signal_type": r.get("signal_type", ""),
                        "status": "",
                        "final_decision": r.get("final_decision", "BUY"),
                        "rule_score": float(r.get("rule_score", 0)),
                        "priority_score": float(r.get("priority_score", 0)),
                        "rule_reasons": r.get("rule_reasons", ""),
                        "llm_decision": r.get("llm_decision", ""),
                        "llm_reason": r.get("llm_reason", ""),
                    }
                    for r in sb_rows
                ])
                records = load_tail_buy_history(
                    run_date=str(run_date or "").strip(),
                    decision=str(decision or "").strip(),
                    limit=limit,
                )
        if not records:
            return {"message": "暂无尾盘策略记录", "records": []}
        simplified = [
            {
                "code": str(r.get("code", "")),
                "name": str(r.get("name", "")),
                "run_date": str(r.get("run_date", "")),
                "signal_type": str(r.get("signal_type", "")),
                "final_decision": str(r.get("final_decision", "")),
                "rule_score": r.get("rule_score", 0),
                "priority_score": r.get("priority_score", 0),
                "llm_decision": str(r.get("llm_decision", "")),
                "llm_reason": str(r.get("llm_reason", "")),
            }
            for r in records
        ]
        return {"total": len(simplified), "records": simplified}
    except Exception as e:
        logger.exception("query_history(tail_buy) error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 15: 回测
# ---------------------------------------------------------------------------

def run_backtest(
    start: str = "",
    end: str = "",
    hold_days: int = 10,
    top_n: int = 3,
    board: str = "main_chinext",
    stop_loss_pct: float = -7.0,
    take_profit_pct: float = 18.0,
    tool_context: ToolContext = None,
) -> dict:
    """回测威科夫五层漏斗策略的历史表现。耗时较长（3-10分钟），会在后台执行。

    基于历史数据模拟漏斗筛选 + 信号触发 → 买入 → 止盈止损退出的完整流程，
    输出胜率、Sharpe 比率、最大回撤等核心指标。

    Args:
        start: 开始日期（YYYY-MM-DD），默认 6 个月前
        end: 结束日期（YYYY-MM-DD），默认昨天
        hold_days: 最大持仓天数（5/10/15/30），默认 10
        top_n: 每日最大候选数（0=不限），默认 3
        board: 股票池 'main_chinext'/'main'/'chinext'/'all'
        stop_loss_pct: 止损百分比（负数），默认 -7.0
        take_profit_pct: 止盈百分比，默认 18.0

    Returns:
        回测结果摘要：胜率、Sharpe、最大回撤、交易笔数等。
    """
    try:
        from datetime import date, timedelta
        from core.backtester import run_backtest as _run_backtest

        _ensure_tushare_token(tool_context)

        if start:
            start_dt = date.fromisoformat(str(start).strip()[:10])
        else:
            start_dt = date.today() - timedelta(days=180)
        if end:
            end_dt = date.fromisoformat(str(end).strip()[:10])
        else:
            end_dt = date.today() - timedelta(days=1)

        hold_days = max(1, min(int(hold_days), 60))
        top_n = max(0, min(int(top_n), 20))
        stop_loss_pct = min(0.0, float(stop_loss_pct))
        take_profit_pct = max(0.0, float(take_profit_pct))

        _trades_df, summary = _run_backtest(
            start_dt=start_dt,
            end_dt=end_dt,
            hold_days=hold_days,
            top_n=top_n,
            board=str(board or "main_chinext").strip(),
            sample_size=0,
            trading_days=320,
            max_workers=8,
            exit_mode="sltp",
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

        return {
            "period": f"{start_dt} ~ {end_dt}",
            "hold_days": hold_days,
            "top_n": top_n,
            "board": board,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "trades": summary.get("trades", 0),
            "win_rate_pct": summary.get("win_rate_pct"),
            "avg_ret_pct": summary.get("avg_ret_pct"),
            "median_ret_pct": summary.get("median_ret_pct"),
            "sharpe_ratio": summary.get("sharpe_ratio"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "portfolio_total_ret_pct": summary.get("portfolio_total_ret_pct"),
            "portfolio_ann_ret_pct": summary.get("portfolio_ann_ret_pct"),
            "max_consecutive_losses": summary.get("max_consecutive_losses"),
        }
    except Exception as e:
        logger.exception("run_backtest error")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 15-18: Agent 标准工具（仅 CLI，Web 端不暴露）
# ---------------------------------------------------------------------------


def exec_command(command: str, timeout: int = 30, tool_context: ToolContext = None) -> dict:
    """在用户本地执行 shell 命令并返回输出。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时秒数，默认 30

    Returns:
        包含 stdout, stderr, returncode 的 dict。
    """
    import subprocess

    timeout = max(1, min(int(timeout), 120))
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.expanduser("~"),
        )
        return {
            "stdout": r.stdout[:8000] + ("...(截断)" if len(r.stdout) > 8000 else ""),
            "stderr": r.stderr[:2000] + ("...(截断)" if len(r.stderr) > 2000 else ""),
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"命令超时（{timeout}s）", "returncode": -1}
    except Exception as e:
        return {"error": str(e)}


def read_file(path: str, encoding: str = "utf-8", tool_context: ToolContext = None) -> dict:
    """读取用户本地文件内容。支持 txt/csv/json/xlsx 等格式，CSV 自动解析为表格预览。

    Args:
        path: 文件绝对路径或 ~ 开头的路径
        encoding: 文件编码，默认 utf-8

    Returns:
        包含 path, size, content 的 dict。CSV 返回 markdown 表格预览。
    """
    import pathlib

    p = pathlib.Path(path).expanduser().resolve()
    if not p.exists():
        return {"error": f"文件不存在: {p}"}
    if not p.is_file():
        return {"error": f"不是文件: {p}"}
    size = p.stat().st_size
    if size > 50 * 1024 * 1024:
        return {"error": f"文件过大 ({size / 1024 / 1024:.1f}MB)，上限 50MB"}

    suffix = p.suffix.lower()
    try:
        if suffix == ".csv":
            import pandas as pd
            df = pd.read_csv(p, encoding=encoding, nrows=50)
            preview = df.to_markdown(index=False)
            return {"path": str(p), "size": size, "rows_total": "≤50(预览)", "content": preview}
        elif suffix in (".xls", ".xlsx"):
            import pandas as pd
            df = pd.read_excel(p, nrows=50)
            preview = df.to_markdown(index=False)
            return {"path": str(p), "size": size, "rows_total": "≤50(预览)", "content": preview}
        elif suffix == ".json":
            import json as _json
            text = p.read_text(encoding=encoding)[:10000]
            try:
                obj = _json.loads(text)
                content = _json.dumps(obj, ensure_ascii=False, indent=2)[:10000]
            except _json.JSONDecodeError:
                content = text
            return {"path": str(p), "size": size, "content": content}
        else:
            text = p.read_text(encoding=encoding)
            return {
                "path": str(p), "size": size,
                "content": text[:10000] + ("...(截断)" if len(text) > 10000 else ""),
            }
    except Exception as e:
        return {"error": f"读取失败: {e}"}


def write_file(path: str, content: str, encoding: str = "utf-8", tool_context: ToolContext = None) -> dict:
    """将内容写入用户本地文件。自动创建父目录。

    Args:
        path: 文件路径
        content: 要写入的内容
        encoding: 文件编码，默认 utf-8

    Returns:
        包含 path, size 的 dict。
    """
    import pathlib

    p = pathlib.Path(path).expanduser().resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return {"path": str(p), "size": p.stat().st_size}
    except Exception as e:
        return {"error": f"写入失败: {e}"}


def web_fetch(url: str, tool_context: ToolContext = None) -> dict:
    """抓取指定 URL 的网页内容并返回纯文本。

    Args:
        url: 要抓取的网页 URL

    Returns:
        包含 url, status, content 的 dict。
    """
    import re

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Wyckoff-Agent/1.0"})
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            text = resp.text[:8000]
        elif "html" in ctype:
            text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()[:8000]
        else:
            text = resp.text[:8000]
        return {"url": url, "status": resp.status_code, "content": text}
    except Exception as e:
        return {"error": f"抓取失败: {e}"}


# ---------------------------------------------------------------------------
# 工具列表导出（Web/Streamlit 端，不含 exec/read/write/web_fetch）
# ---------------------------------------------------------------------------

WYCKOFF_TOOLS = [
    search_stock_by_name,
    analyze_stock,
    portfolio,
    get_market_overview,
    screen_stocks,
    generate_ai_report,
    generate_strategy_decision,
    query_history,
    update_portfolio,
    run_backtest,
]
