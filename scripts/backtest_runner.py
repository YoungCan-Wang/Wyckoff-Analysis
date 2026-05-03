"""
日线级轻量回测器（低成本数据版）

目标：
1) 复用当前 Wyckoff Funnel 规则，不依赖分钟级或付费 Level-2 数据。
2) 在给定历史区间内，统计信号后 N 交易日收益分布与胜率。
3) 输出 summary markdown + trades csv，便于后续参数复盘。

重要说明：
- 默认按生产口径开启“当前截面市值/行业映射”过滤，便于回测结果对齐实盘行为。
- 仍存在幸存者偏差（股票池基于当前在市样本），结果用于参数对比而非绝对收益承诺。
"""

from __future__ import annotations

import argparse
import bisect
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.funnel_pipeline import (
    analyze_benchmark_and_tune_cfg as _tune_cfg_by_regime,
)
from core.funnel_pipeline import (
    calc_market_breadth as _calc_market_breadth_for_regime,
)
from core.funnel_pipeline import (
    rank_l3_candidates,
)
from core.sector_rotation import analyze_sector_rotation
from core.signal_confirmation import PendingPool
from core.wyckoff_engine import (
    FunnelConfig,
    FunnelResult,
    allocate_ai_candidates,
    normalize_hist_from_fetch,
    run_funnel,
)
from integrations.data_source import fetch_index_hist, fetch_market_cap_map, fetch_sector_map, fetch_stock_hist
from integrations.fetch_a_share_csv import _normalize_symbols, get_stocks_by_board
from tools.funnel_config import apply_funnel_cfg_overrides as _shared_apply_funnel_cfg_overrides

DEFAULT_HOLD_DAYS = 30  # 网格优化：30天夏普2.493 > 25天1.967 > 20天1.413
DEFAULT_EXIT_MODE = "sltp"
DEFAULT_STOP_LOSS_PCT = -7.0  # 网格优化最佳：SL7/TP18（夏普1.928 > SL6/TP15的1.679 > SL8/TP20的1.466）
DEFAULT_TAKE_PROFIT_PCT = 18.0
DEFAULT_TRAILING_STOP_PCT = 0.0  # 0 = 不启用移动止盈；如 -5.0 表示从最高点回撤 5% 卖出
DEFAULT_TRAILING_ACTIVATE_PCT = 0.0  # 移动止盈激活门槛(%)，如 10.0 表示浮盈 ≥10% 后才启用移动止盈

# ── ATR 模式常量（对齐实盘 step4_rebalancer） ──
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_MULTIPLIER = 2.0  # 实盘 STEP4_ATR_MULTIPLIER = 2.0
DEFAULT_ATR_HARD_STOP_PCT = -9.0  # 极限止损地板(%)，实盘 STEP4_BUY_HARD_STOP_PCT = 9.0
DEFAULT_ATR_MAX_HOLD_DAYS = 120  # ATR 模式下最大持有天数（安全网）

DEFAULT_USE_CURRENT_META = True
DEFAULT_BUY_FRICTION_PCT = float(os.getenv("BACKTEST_BUY_FRICTION_PCT", "0.5"))
DEFAULT_SELL_FRICTION_PCT = float(os.getenv("BACKTEST_SELL_FRICTION_PCT", "0.5"))
DEFAULT_METRICS_ENGINE = os.getenv("BACKTEST_METRICS_ENGINE", "legacy").strip().lower() or "legacy"
DEFAULT_WBT_FEE_RATE = float(os.getenv("BACKTEST_WBT_FEE_RATE", "0.0"))
DEFAULT_WBT_N_JOBS = int(os.getenv("BACKTEST_WBT_N_JOBS", "1"))
BACKTEST_CACHE_ONLY_FIRST = os.getenv("BACKTEST_CACHE_ONLY_FIRST", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ── 大盘水温仓位控制 ──
# 回测数据显示 NEUTRAL 下策略盈利（+1.17%），CRASH/RISK_ON 下亏损严重。
# 通过 regime 动态调节每日候选上限（相当于仓位控制），减少逆势开仓。
REGIME_POSITION_RATIO: dict[str, float] = {
    "NEUTRAL": 1.0,  # 震荡市 → 全仓（回测显示唯一盈利环境）
    "RISK_ON": 0.2,  # 热点追涨期反转率高 → 轻仓试探（回测 Sharpe -0.88，大幅缩仓）
    "PANIC_REPAIR": 0.5,  # 恐慌修复 → 半仓试探
    "RISK_OFF": 0.2,  # 避险 → 轻仓（回测 Sharpe -0.48）
    "CRASH": 0.0,  # 崩盘 → 不开仓
}
FUNNEL_AI_SELECTION_MODE = os.getenv("FUNNEL_AI_SELECTION_MODE", "legacy_full_hits").strip().lower()
_LEGACY_SELECTION_MODES = {
    "legacy_full_hits",
    "legacy_hits",
    "all_hits",
    "classic",
}


@dataclass
class TradeRecord:
    signal_date: date
    entry_date: date | None
    exit_date: date
    code: str
    name: str
    trigger: str
    score: float
    entry_close: float
    exit_close: float
    ret_pct: float
    track: str = ""  # "Trend" / "Accum" / "" (unclassified)
    regime: str = ""  # market regime at signal time


def _parse_date(v: str) -> date:
    s = str(v).strip().replace("/", "-")
    if "-" in s:
        return datetime.strptime(s, "%Y-%m-%d").date()
    return datetime.strptime(s, "%Y%m%d").date()


def _parse_hold_days_list(raw: str) -> list[int]:
    vals: list[int] = []
    for token in str(raw or "").replace("，", ",").replace(" ", ",").split(","):
        t = str(token).strip()
        if not t:
            continue
        n = int(t)
        if n <= 0:
            raise ValueError(f"hold_days_list 中存在非法值: {n}")
        vals.append(n)
    dedup = sorted(set(vals))
    if not dedup:
        raise ValueError("hold_days_list 为空")
    return dedup


def _normalize_backtest_board(board: str) -> str:
    b = str(board or "").strip().lower()
    # 回测统一口径：all 兼容映射到主板+创业板
    if b in {"", "all"}:
        return "main_chinext"
    return b


def _is_main_code(code: str) -> bool:
    return str(code or "").startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def _is_chinext_code(code: str) -> bool:
    return str(code or "").startswith(("300", "301"))


def _board_match(code: str, board: str) -> bool:
    b = _normalize_backtest_board(board)
    c = str(code or "").strip()
    if b == "main":
        return _is_main_code(c)
    if b == "chinext":
        return _is_chinext_code(c)
    # main_chinext（默认）以及未知值的兜底
    return _is_main_code(c) or _is_chinext_code(c)


def _build_universe(board: str, sample_size: int) -> tuple[list[str], dict[str, str]]:
    board_norm = _normalize_backtest_board(board)
    if board_norm == "main":
        items = get_stocks_by_board("main")
    elif board_norm == "chinext":
        items = get_stocks_by_board("chinext")
    else:
        items = get_stocks_by_board("main_chinext")

    name_map = {
        str(x.get("code", "")).strip(): str(x.get("name", "")).strip() for x in items if str(x.get("code", "")).strip()
    }
    # 过滤 ST 后采样（可复现）
    symbols = [
        s
        for s in _normalize_symbols(list(name_map.keys()))
        if _board_match(s, board_norm) and "ST" not in name_map.get(s, "").upper()
    ]
    symbols = sorted(set(symbols))
    if sample_size > 0:
        symbols = symbols[:sample_size]
    return symbols, name_map


def _load_snapshot_hist_map(
    snapshot_dir: Path,
    symbols_filter: set[str] | None = None,
) -> tuple[dict[str, pd.DataFrame], int]:
    full_path = snapshot_dir / "hist_full.csv.gz"
    if not full_path.exists():
        raise FileNotFoundError(f"snapshot missing file: {full_path}")
    # Keep stock codes as strings; otherwise pandas may drop leading zeros (000001 -> 1).
    df = pd.read_csv(
        full_path,
        compression="gzip",
        low_memory=False,
        dtype={"symbol": str},
    )
    if df.empty:
        return {}, 0
    if "symbol" not in df.columns:
        raise RuntimeError(f"snapshot file missing symbol column: {full_path}")

    if symbols_filter:
        df = df[df["symbol"].astype(str).isin(symbols_filter)]
    if df.empty:
        return {}, 0

    keep_cols = [
        c for c in ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg"] if c in df.columns
    ]
    df = df[keep_cols].copy()
    df["symbol"] = df["symbol"].astype(str).str.strip().str.zfill(6)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["symbol", "date"]).reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    out: dict[str, pd.DataFrame] = {}
    for sym, g in df.groupby("symbol", sort=False):
        one = g.drop(columns=["symbol"]).sort_values("date").reset_index(drop=True)
        if not one.empty:
            out[sym] = one
    return out, int(len(df))


def _load_snapshot_benchmark(
    snapshot_dir: Path,
) -> pd.DataFrame | None:
    bench_path = snapshot_dir / "benchmark_main.csv"
    if not bench_path.exists():
        return None
    df = pd.read_csv(bench_path, low_memory=False)
    if df.empty or "date" not in df.columns:
        return None
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out if not out.empty else None


def _load_snapshot_name_map(snapshot_dir: Path) -> dict[str, str] | None:
    """从快照加载股票列表 {code: name}，Phase 1 导出。"""
    p = snapshot_dir / "name_map.json"
    if not p.exists():
        return None
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return None


def _load_snapshot_sector_map(snapshot_dir: Path) -> dict[str, str] | None:
    """从快照加载行业映射 {code: industry}，Phase 1 导出。"""
    p = snapshot_dir / "sector_map.json"
    if not p.exists():
        return None
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return None


def _load_snapshot_market_cap_map(snapshot_dir: Path) -> dict[str, float] | None:
    """从快照加载市值映射 {code: total_mv_亿}，Phase 1 导出。"""
    p = snapshot_dir / "market_cap_map.json"
    if not p.exists():
        return None
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception:
        pass
    return None


def _apply_funnel_cfg_overrides(cfg: FunnelConfig) -> None:
    """
    与生产漏斗同口径：读取 FUNNEL_CFG_* 环境变量覆盖 FunnelConfig。
    """
    _shared_apply_funnel_cfg_overrides(cfg)


def _fetch_hist_norm(
    symbol: str,
    start_dt: date,
    end_dt: date,
) -> tuple[str, pd.DataFrame | None, str | None]:
    try:
        # 与生产口径对齐：默认允许 stock_repo 补缺口（cache_only=False）。
        # 若需提速可设置 BACKTEST_CACHE_ONLY_FIRST=1 优先只读缓存。
        try:
            from integrations.stock_hist_repository import get_stock_hist as _cached

            raw = None
            if BACKTEST_CACHE_ONLY_FIRST:
                raw = _cached(
                    symbol=symbol,
                    start_date=start_dt,
                    end_date=end_dt,
                    adjust="qfq",
                    context="background",
                    cache_only=True,
                )
                if raw is None or raw.empty:
                    raw = _cached(
                        symbol=symbol,
                        start_date=start_dt,
                        end_date=end_dt,
                        adjust="qfq",
                        context="background",
                        cache_only=False,
                    )
            else:
                raw = _cached(
                    symbol=symbol,
                    start_date=start_dt,
                    end_date=end_dt,
                    adjust="qfq",
                    context="background",
                    cache_only=False,
                )
        except Exception:
            raw = None
        # 兜底：repo 异常或无数据时直连数据源
        if raw is None or raw.empty:
            raw = fetch_stock_hist(symbol, start_dt, end_dt, adjust="qfq")
        df = normalize_hist_from_fetch(raw)
        if df is None or df.empty:
            return symbol, None, "empty"
        out = df.sort_values("date").copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
        out = out.dropna(subset=["date"]).reset_index(drop=True)
        if out.empty:
            return symbol, None, "empty_after_date_parse"
        return symbol, out, None
    except Exception as exc:  # pragma: no cover - runtime path
        return symbol, None, str(exc)


def _combine_trigger_scores(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, tuple[float, str]]:
    """
    合并 spring/lps/evr 触发结果：
    返回 code -> (best_score, joined_trigger_name)
    """
    reason_map: dict[str, list[str]] = {}
    score_map: dict[str, float] = {}
    for key, pairs in triggers.items():
        for code, score in pairs:
            if code not in reason_map:
                reason_map[code] = []
                score_map[code] = float(score)
            reason_map[code].append(key)
            score_map[code] = max(score_map.get(code, 0.0), float(score))
    out: dict[str, tuple[float, str]] = {}
    for code, reasons in reason_map.items():
        out[code] = (score_map.get(code, 0.0), "、".join(reasons))
    return out


def _dedup_order(codes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = str(raw).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _select_ai_input_codes(
    *,
    result: FunnelResult,
    day_df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    regime: str,
    selection_mode: str,
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    """
    按线上漏斗口径选出“送给 AI 的候选池”：
    - legacy_full_hits：全量 L4 命中，按触发分值排序
    - modern quotas：L3 排序 + allocate_ai_candidates 动态配额
    返回 (selected_codes, priority_score_map, track_map)
    """
    merged_trigger_map = _combine_trigger_scores(result.triggers)
    hit_score_map = {code: float(v[0]) for code, v in merged_trigger_map.items()}
    sorted_hit_codes = sorted(
        merged_trigger_map.keys(),
        key=lambda c: -hit_score_map.get(c, 0.0),
    )

    sos_hit_set = set(str(c).strip() for c, _ in result.triggers.get("sos", []))
    evr_hit_set = set(str(c).strip() for c, _ in result.triggers.get("evr", []))
    spring_hit_set = set(str(c).strip() for c, _ in result.triggers.get("spring", []))
    lps_hit_set = set(str(c).strip() for c, _ in result.triggers.get("lps", []))

    if selection_mode in _LEGACY_SELECTION_MODES:
        track_map = {}
        for code in sorted_hit_codes:
            if code in sos_hit_set or code in evr_hit_set:
                track_map[code] = "Trend"
            elif code in spring_hit_set or code in lps_hit_set:
                track_map[code] = "Accum"
            else:
                track_map[code] = "Trend"
        return sorted_hit_codes, hit_score_map, track_map

    sector_rotation = analyze_sector_rotation(
        day_df_map,
        sector_map,
        universe_symbols=list(day_df_map.keys()),
        focus_sectors=result.top_sectors,
    )
    sector_rotation_map = (sector_rotation or {}).get("state_map", {}) or {}
    l3_ranked_symbols, _ = rank_l3_candidates(
        l3_symbols=result.layer3_symbols,
        df_map=day_df_map,
        sector_map=sector_map,
        triggers=result.triggers,
        top_sectors=result.top_sectors,
        l2_channel_map=result.channel_map,
        sector_rotation_map=sector_rotation_map,
    )
    trend_sel, accum_sel, priority_score_map = allocate_ai_candidates(
        result,
        l3_ranked_symbols or result.layer3_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=2,
    )
    selected_codes = _dedup_order(trend_sel + accum_sel)
    min_score = float(getattr(FunnelConfig, "min_funnel_score", 0.15) or 0)
    if min_score > 0 and priority_score_map:
        selected_codes = [c for c in selected_codes if priority_score_map.get(c, 0.0) >= min_score]
    track_map = dict.fromkeys(trend_sel, "Trend")
    track_map.update(dict.fromkeys(accum_sel, "Accum"))
    return selected_codes, priority_score_map, track_map


def _close_on_date(df: pd.DataFrame, d: date) -> float | None:
    row = df[df["date"] == d]
    if row.empty:
        return None
    v = pd.to_numeric(row["close"], errors="coerce").dropna()
    if v.empty:
        return None
    return float(v.iloc[-1])


def _close_on_or_after(df: pd.DataFrame, d: date) -> tuple[float | None, date | None]:
    row = df[df["date"] >= d].head(1)
    if row.empty:
        return None, None
    v = pd.to_numeric(row["close"], errors="coerce").dropna()
    if v.empty:
        return None, None
    hit_date = row.iloc[0]["date"]
    return float(v.iloc[0]), hit_date


def _is_limit_up_locked(row_s: pd.Series) -> bool:
    """判断是否为一字涨停（open==high==low 且较前日上涨），无法买入。"""
    try:
        o = float(row_s.get("open", 0))
        h = float(row_s.get("high", 0))
        lo = float(row_s.get("low", 0))
        c = float(row_s.get("close", 0))
        if o <= 0:
            return False
        # 一字板：开盘=最高=最低（允许微小浮点误差）
        tol = o * 1e-6
        if abs(h - o) <= tol and abs(lo - o) <= tol:
            return c >= o  # 涨停方向
    except (TypeError, ValueError):
        pass
    return False


def _open_on_or_after(df: pd.DataFrame, d: date) -> tuple[float | None, date | None]:
    """取目标日期（含）之后首个可成交交易日的开盘价，跳过一字涨停日。"""
    candidates = df[df["date"] >= d].head(5)
    if candidates.empty:
        return None, None
    for _, row_s in candidates.iterrows():
        if _is_limit_up_locked(row_s):
            continue
        if "open" in candidates.columns:
            v = pd.to_numeric(pd.Series([row_s["open"]]), errors="coerce").dropna()
            if not v.empty:
                return float(v.iloc[0]), row_s["date"]
        v = pd.to_numeric(pd.Series([row_s["close"]]), errors="coerce").dropna()
        if not v.empty:
            return float(v.iloc[0]), row_s["date"]
    return None, None


def _close_on_or_before(
    df: pd.DataFrame,
    d: date,
    lower_exclusive: date | None = None,
) -> tuple[float | None, date | None]:
    row = df[df["date"] <= d]
    if lower_exclusive is not None:
        row = row[row["date"] > lower_exclusive]
    if row.empty:
        return None, None
    row = row.tail(1)
    v = pd.to_numeric(row["close"], errors="coerce").dropna()
    if v.empty:
        return None, None
    hit_date = row.iloc[0]["date"]
    return float(v.iloc[0]), hit_date


def _build_daily_ohlc_lookup(
    df: pd.DataFrame,
) -> dict[date, tuple[float, float, float, float]]:
    out: dict[date, tuple[float, float, float, float]] = {}
    if df is None or df.empty:
        return out

    cols = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
    if "date" not in cols or "close" not in cols:
        return out

    work = df[cols].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    for c in ["open", "high", "low", "close"]:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work.dropna(subset=["date", "close"])

    for row in work.itertuples(index=False):
        d = row.date
        close_v = float(row.close)
        open_v = float(row.open) if hasattr(row, "open") and pd.notna(row.open) else close_v
        high_v = float(row.high) if hasattr(row, "high") and pd.notna(row.high) else max(open_v, close_v)
        low_v = float(row.low) if hasattr(row, "low") and pd.notna(row.low) else min(open_v, close_v)
        out[d] = (open_v, high_v, low_v, close_v)
    return out


def _calc_atr_from_ohlc(
    sorted_dates: list[date],
    day_ohlc: dict[date, tuple[float, float, float, float]],
    as_of: date,
    period: int = 14,
) -> float | None:
    """从预排序日期列表 + OHLC lookup 计算截止 as_of 的 ATR（SMA of TR）。

    复用 step4_rebalancer._calc_atr 的逻辑（SMA，非 Wilder EMA）。
    sorted_dates 由调用方一次性排序并传入以避免重复排序。
    """
    right = bisect.bisect_right(sorted_dates, as_of)
    if right < period + 1:
        return None
    window = sorted_dates[right - period - 1 : right]
    trs: list[float] = []
    for i in range(1, len(window)):
        _, h, l, _ = day_ohlc[window[i]]
        _, _, _, prev_c = day_ohlc[window[i - 1]]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs) if trs else None


def run_backtest(
    start_dt: date,
    end_dt: date,
    hold_days: int,
    top_n: int,
    board: str,
    sample_size: int,
    trading_days: int,
    max_workers: int,
    snapshot_dir: Path | None = None,
    exit_mode: str = DEFAULT_EXIT_MODE,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    trailing_stop_pct: float = DEFAULT_TRAILING_STOP_PCT,
    trailing_activate_pct: float = DEFAULT_TRAILING_ACTIVATE_PCT,
    sltp_priority: str = "stop_first",
    use_current_meta: bool = DEFAULT_USE_CURRENT_META,
    buy_friction_pct: float = DEFAULT_BUY_FRICTION_PCT,
    sell_friction_pct: float = DEFAULT_SELL_FRICTION_PCT,
    regime_filter: bool = False,
    pending_mode: str = "both",
    pending_merge_order: str = "funnel_first",
    atr_period: int = DEFAULT_ATR_PERIOD,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    atr_hard_stop_pct: float = DEFAULT_ATR_HARD_STOP_PCT,
    metrics_engine: str = DEFAULT_METRICS_ENGINE,
    wbt_fee_rate: float = DEFAULT_WBT_FEE_RATE,
    wbt_n_jobs: int = DEFAULT_WBT_N_JOBS,
) -> tuple[pd.DataFrame, dict]:
    metrics_engine = str(metrics_engine or "legacy").strip().lower()
    if metrics_engine not in {"legacy", "auto", "both", "wbt"}:
        raise ValueError("metrics_engine 必须是 legacy / auto / both / wbt")
    if pending_mode not in {"off", "only", "both"}:
        raise ValueError("pending_mode 必须是 off / only / both")
    if pending_merge_order not in {"funnel_first", "confirmed_first"}:
        raise ValueError("pending_merge_order 必须是 funnel_first 或 confirmed_first")
    if end_dt <= start_dt:
        raise ValueError("end 必须晚于 start")
    if hold_days < 1:
        raise ValueError("hold_days 必须 >= 1")
    if exit_mode not in {"close_only", "sltp", "atr"}:
        raise ValueError("exit_mode 必须是 close_only、sltp 或 atr")
    if sltp_priority not in {"stop_first", "take_first"}:
        raise ValueError("sltp_priority 必须是 stop_first 或 take_first")
    if trailing_stop_pct > 0:
        raise ValueError("trailing_stop_pct 必须 <= 0（如 -5.0 表示从最高点回撤 5%），0 表示不启用")
    if trailing_activate_pct < 0:
        raise ValueError("trailing_activate_pct 必须 >= 0（如 10.0 表示浮盈 10% 后激活），0 表示立即启用")
    if stop_loss_pct > 0:
        raise ValueError("stop_loss_pct 必须 <= 0，0 表示不设止损")
    if take_profit_pct < 0:
        raise ValueError("take_profit_pct 必须 >= 0，0 表示不设止盈")
    if buy_friction_pct < 0 or sell_friction_pct < 0:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 >= 0")
    if buy_friction_pct >= 100 or sell_friction_pct >= 100:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 < 100")
    if wbt_fee_rate < 0:
        raise ValueError("wbt_fee_rate 必须 >= 0")
    if wbt_n_jobs < 1:
        raise ValueError("wbt_n_jobs 必须 >= 1")

    # ── 快照模式：优先从快照加载股票列表，避免网络调用 ──
    snapshot_name_map: dict[str, str] | None = None
    if snapshot_dir is not None:
        snapshot_dir = Path(snapshot_dir).resolve()
        snapshot_name_map = _load_snapshot_name_map(snapshot_dir)

    if snapshot_name_map is not None:
        # 从快照的 name_map 派生 symbols（零网络调用）
        name_map = snapshot_name_map
        all_codes = sorted(name_map.keys())
        # ST 过滤 + 采样，与 _build_universe 保持同口径
        symbols = [
            s
            for s in _normalize_symbols(all_codes)
            if _board_match(s, board) and "ST" not in name_map.get(s, "").upper()
        ]
        if sample_size > 0:
            symbols = symbols[:sample_size]
        print(f"[backtest] 股票池={len(symbols)} (快照 name_map, board={board}, sample_size={sample_size})")
    else:
        symbols, name_map = _build_universe(board=board, sample_size=sample_size)
        print(f"[backtest] 股票池={len(symbols)} (网络拉取, board={board}, sample_size={sample_size})")
    from cli.progress import report_progress

    report_progress("股票池建立", f"共{len(symbols)}只", 0.0)
    if not symbols:
        raise RuntimeError("股票池为空")

    prefetch_start = start_dt - timedelta(days=trading_days * 3)
    prefetch_end = end_dt + timedelta(days=hold_days * 3 + 30)

    all_df_map: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    bench_df: pd.DataFrame | None = None
    snapshot_rows_total = 0
    snapshot_used = False

    if snapshot_dir is not None:
        print(f"[backtest] 使用本地快照: {snapshot_dir}")
        all_df_map, snapshot_rows_total = _load_snapshot_hist_map(snapshot_dir, symbols_filter=set(symbols))
        bench_df = _load_snapshot_benchmark(snapshot_dir)
        snapshot_used = True
        if not all_df_map:
            raise RuntimeError(f"快照无可用历史数据: {snapshot_dir}")
        print(f"[backtest] 快照载入完成: ok={len(all_df_map)}, rows={snapshot_rows_total}")
    else:
        print(f"[backtest] 开始拉取历史日线: symbols={len(symbols)}, workers={max_workers}")
        report_progress("拉取历史", f"共{len(symbols)}只", 0.0)
        with ThreadPoolExecutor(max_workers=max(int(max_workers), 1)) as ex:
            futures = {ex.submit(_fetch_hist_norm, sym, prefetch_start, prefetch_end): sym for sym in symbols}
            done = 0
            for ft in as_completed(futures):
                done += 1
                sym = futures[ft]
                code, df, err = ft.result()
                if df is not None and not df.empty:
                    all_df_map[code] = df
                else:
                    failures.append(f"{sym}:{err or 'unknown'}")
                if done % 200 == 0 or done == len(futures):
                    print(f"[backtest] 拉取进度 {done}/{len(futures)}")
                    report_progress("拉取历史", f"{done}/{len(futures)}", done / len(futures) * 0.4)
        print(f"[backtest] 历史拉取完成: ok={len(all_df_map)}, fail={len(failures)}")
        report_progress("拉取完成", f"成功={len(all_df_map)}", 0.4)

    if bench_df is None or bench_df.empty:
        try:
            bench_raw = fetch_index_hist("000001", prefetch_start, prefetch_end)
        except Exception as exc:
            raise RuntimeError("回测需要大盘交易日历与基准收益，请先配置可用的 TUSHARE_TOKEN。") from exc
        bench_df = normalize_hist_from_fetch(bench_raw).sort_values("date").copy()
        bench_df["date"] = pd.to_datetime(bench_df["date"], errors="coerce").dt.date
        bench_df = bench_df.dropna(subset=["date"]).reset_index(drop=True)

    trade_dates = [d for d in bench_df["date"].tolist() if start_dt <= d <= end_dt]
    print(
        f"[backtest] DEBUG: start={start_dt}, end={end_dt}, bench_min={bench_df['date'].min()}, bench_max={bench_df['date'].max()}"
    )
    print(f"[backtest] DEBUG: trade_dates count={len(trade_dates)}")
    if len(trade_dates) <= hold_days + 1:
        raise RuntimeError(
            f"回测区间交易日过少({len(trade_dates)})，无法计算 forward return (hold_days={hold_days}，需至少 {hold_days + 2} 个交易日)"
        )

    if use_current_meta:
        # 快照优先：从快照加载 sector_map / market_cap_map（Phase 1 已导出）
        # 仅在快照不存在时 fallback 到网络拉取
        _snap_sector = _load_snapshot_sector_map(snapshot_dir) if snapshot_dir is not None else None
        _snap_cap = _load_snapshot_market_cap_map(snapshot_dir) if snapshot_dir is not None else None

        if _snap_sector is not None or _snap_cap is not None:
            sector_map = _snap_sector or {}
            market_cap_map = _snap_cap or {}
            print(f"[backtest] 元数据从快照加载: sector_map={len(sector_map)}, market_cap_map={len(market_cap_map)}")
        else:
            market_cap_map = fetch_market_cap_map()
            sector_map = fetch_sector_map()
            print("[backtest] ⚠️ 使用当前截面市值/行业映射（会引入 look-ahead bias）")
        if not market_cap_map:
            print("[backtest] ⚠️ 当前市值映射为空，Layer1 市值过滤将被跳过")
    else:
        market_cap_map = {}
        sector_map = {}
        print("[backtest] 偏差抑制口径：关闭当前截面市值/行业映射过滤 (L1 市值过滤 + L3 行业共振过滤)")
    base_cfg = FunnelConfig(trading_days=trading_days)
    _apply_funnel_cfg_overrides(base_cfg)

    records: list[TradeRecord] = []
    signal_days = 0
    eval_days = 0
    ohlc_lookup_cache: dict[str, dict[date, tuple[float, float, float, float]]] = {}

    pending_pool = PendingPool() if pending_mode != "off" else None
    pending_confirmed_total = 0

    max_idx = len(trade_dates) - hold_days - 1  # -1: 信号次日才能入场，需多预留一天
    for idx in range(max_idx):
        signal_date = trade_dates[idx]
        entry_target_date = trade_dates[idx + 1]  # 信号日收盘后才能看到信号，次日开盘才能买入
        trade_dates[idx + 1 + hold_days]  # 从实际入场日起计算持有天数

        # 各票截止到 signal_date 的切片（滚动窗口）
        day_df_map: dict[str, pd.DataFrame] = {}
        for code, df in all_df_map.items():
            s = df[df["date"] <= signal_date]
            if s.empty:
                continue
            tail = s.tail(trading_days)
            if len(tail) < base_cfg.ma_long:
                continue
            day_df_map[code] = tail
        if not day_df_map:
            continue

        bench_slice = bench_df[bench_df["date"] <= signal_date].tail(trading_days)
        if len(bench_slice) < base_cfg.ma_long:
            continue

        # 回测与实盘同构：按“当日”市场状态动态调参，避免静态 cfg 导致口径漂移。
        day_cfg = replace(base_cfg)
        day_breadth = _calc_market_breadth_for_regime(day_df_map)
        bench_context = _tune_cfg_by_regime(
            bench_slice,
            None,
            day_cfg,
            breadth=day_breadth,
        )

        eval_days += 1
        result = run_funnel(
            all_symbols=list(day_df_map.keys()),
            df_map=day_df_map,
            bench_df=bench_slice,
            name_map=name_map,
            market_cap_map=market_cap_map,
            sector_map=sector_map,
            cfg=day_cfg,
        )

        regime = bench_context.get("regime", "NEUTRAL") if bench_context else "NEUTRAL"
        signal_date_str = signal_date.isoformat()

        confirmed_codes: list[str] = []
        confirmed_score_map: dict[str, float] = {}
        confirmed_track_map: dict[str, str] = {}
        if pending_pool is not None:
            pending_pool.write(signal_date_str, result.triggers, day_df_map, regime, name_map, sector_map, day_cfg)
            for cs in pending_pool.tick(day_df_map, signal_date_str):
                c = str(cs.get("code", "")).strip()
                if c:
                    confirmed_codes.append(c)
                    confirmed_score_map[c] = float(cs.get("score", 0))
                    confirmed_track_map[c] = str(cs.get("track", "Trend"))
            pending_confirmed_total += len(confirmed_codes)

        selected_for_ai, p_score_map, track_map = _select_ai_input_codes(
            result=result,
            day_df_map=day_df_map,
            sector_map=sector_map,
            regime=regime,
            selection_mode=FUNNEL_AI_SELECTION_MODE,
        )

        if pending_mode == "only":
            if not confirmed_codes:
                continue
            ranked_codes = confirmed_codes
            p_score_map.update(confirmed_score_map)
            track_map.update(confirmed_track_map)
        elif pending_mode == "both":
            # 对齐生产链路顺序（Step2 候选在前，Step2.5 confirmed 追加）
            if pending_merge_order == "confirmed_first":
                seen = set(confirmed_codes)
                merged = list(confirmed_codes) + [c for c in selected_for_ai if c not in seen]
            else:
                seen = set(selected_for_ai)
                merged = list(selected_for_ai) + [c for c in confirmed_codes if c not in seen]
            if not merged:
                continue
            ranked_codes = merged if int(top_n) <= 0 else merged[:top_n]
            p_score_map.update(confirmed_score_map)
            track_map.update(confirmed_track_map)
        else:
            if not selected_for_ai:
                continue
            ranked_codes = selected_for_ai if int(top_n) <= 0 else selected_for_ai[:top_n]

        if regime_filter and ranked_codes:
            ratio = REGIME_POSITION_RATIO.get(regime, 1.0)
            if ratio <= 0:
                continue
            if ratio < 1.0:
                keep_n = max(1, int(len(ranked_codes) * ratio + 0.5))
                ranked_codes = ranked_codes[:keep_n]

        # Only needed for string names
        name_score_map = _combine_trigger_scores(result.triggers)

        signal_days += 1
        for code in ranked_codes:
            full_df = all_df_map.get(code)
            if full_df is None or full_df.empty:
                continue
            # 核心修正：实盘中信号出现在收盘后，最早只能在次日开盘买入
            # 停牌股可能延后成交，必须用 actual_entry_date 计算持有窗口
            entry_close, actual_entry_date = _open_on_or_after(full_df, entry_target_date)
            if entry_close is None or entry_close <= 0 or actual_entry_date is None:
                continue

            # 根据实际成交日推算退出锚点和市场窗口（停牌股的实际入场日可能晚于 entry_target_date）
            try:
                actual_entry_idx = trade_dates.index(actual_entry_date)
            except ValueError:
                # actual_entry_date 不在基准交易日列表中（极端情况：个股复牌日不在大盘交易日内）
                actual_entry_idx = idx + 1  # fallback 到原始逻辑
            # ATR 模式使用更长的持有窗口（安全网），其余模式用 hold_days
            effective_max_hold = DEFAULT_ATR_MAX_HOLD_DAYS if exit_mode == "atr" else hold_days
            actual_exit_idx = actual_entry_idx + effective_max_hold
            if actual_exit_idx >= len(trade_dates):
                if exit_mode == "atr":
                    actual_exit_idx = len(trade_dates) - 1  # ATR 模式截断到可用范围
                else:
                    continue  # sltp/close_only 模式：剩余交易日不足以覆盖完整持有期
            actual_exit_anchor = trade_dates[actual_exit_idx]

            if exit_mode == "close_only":
                # 兼容旧口径：持有 N 个市场交易日后按 anchor 日（或其后首个可得日）收盘离场。
                exit_close, exit_date = _close_on_or_after(full_df, actual_exit_anchor)

            elif exit_mode == "sltp":
                # sltp 口径：T+1 合规，从入场次日起检查止盈止损。
                exit_close = None
                exit_date = None
                market_window = trade_dates[actual_entry_idx + 1 : actual_exit_idx + 1]
                day_ohlc = ohlc_lookup_cache.get(code)
                if day_ohlc is None:
                    day_ohlc = _build_daily_ohlc_lookup(full_df)
                    ohlc_lookup_cache[code] = day_ohlc

                sl_price = entry_close * (1.0 + stop_loss_pct / 100.0) if stop_loss_pct < 0 else None
                tp_price = entry_close * (1.0 + take_profit_pct / 100.0) if take_profit_pct > 0 else None
                use_trailing = trailing_stop_pct < 0
                trailing_activated = trailing_activate_pct <= 0  # 门槛 ≤0 表示立即激活
                activate_price = entry_close * (1.0 + trailing_activate_pct / 100.0) if not trailing_activated else 0.0
                peak_high = entry_close  # 持仓期间最高价，用于移动止盈

                prev_close_sltp = entry_close
                for mkt_day in market_window:
                    candle = day_ohlc.get(mkt_day)
                    if candle is None:
                        continue
                    open_px, high, low, _ = candle

                    # 一字跌停：无法卖出，跳过（open==high==low 且较前日下跌）
                    tol = open_px * 1e-6
                    if (
                        open_px > 0
                        and abs(high - open_px) <= tol
                        and abs(low - open_px) <= tol
                        and open_px < prev_close_sltp
                    ):
                        prev_close_sltp = candle[3]
                        continue
                    prev_close_sltp = candle[3]

                    # 激活门槛：浮盈达到 trailing_activate_pct 后才启用移动止盈
                    if use_trailing and not trailing_activated and high >= activate_price:
                        trailing_activated = True

                    # 移动止盈线基于昨日 peak_high 计算（避免同根K线悖论：
                    # 当日最高价刷新 peak 的同时当日最低价触发回撤，逻辑自相矛盾）
                    trailing_price = (
                        peak_high * (1.0 + trailing_stop_pct / 100.0) if use_trailing and trailing_activated else None
                    )

                    # 检查顺序：固定止损 → 移动止盈 → 固定止盈
                    # （先保命、再锁利、最后达标止盈）
                    if sltp_priority == "stop_first":
                        checks = [("sl", sl_price), ("trail", trailing_price), ("tp", tp_price)]
                    else:
                        checks = [("tp", tp_price), ("trail", trailing_price), ("sl", sl_price)]

                    hit = False
                    for kind, px in checks:
                        if px is None:
                            continue
                        if kind == "sl" and low <= px:
                            exit_close = px if open_px >= px else open_px
                            exit_date = mkt_day
                            hit = True
                            break
                        if kind == "trail" and low <= px:
                            exit_close = px if open_px >= px else open_px
                            exit_date = mkt_day
                            hit = True
                            break
                        if kind == "tp" and high >= px:
                            exit_close = px if open_px <= px else open_px
                            exit_date = mkt_day
                            hit = True
                            break
                    if hit:
                        break

                    # 检查完毕后再更新 peak_high（放在 break 之后确保不影响当日判定）
                    peak_high = max(peak_high, high)

                if exit_close is None:
                    # 未触发则按窗口最后一天(含)及之前最近可得收盘离场，不延长持仓天数。
                    exit_close, exit_date = _close_on_or_before(
                        full_df,
                        actual_exit_anchor,
                        lower_exclusive=signal_date,
                    )

            elif exit_mode == "atr":
                # ATR 模式：对齐实盘 step4_rebalancer 的 ATR 动态止损 + trailing。
                # T+1 合规，从入场次日起检查。
                exit_close = None
                exit_date = None
                market_window = trade_dates[actual_entry_idx + 1 : actual_exit_idx + 1]
                day_ohlc = ohlc_lookup_cache.get(code)
                if day_ohlc is None:
                    day_ohlc = _build_daily_ohlc_lookup(full_df)
                    ohlc_lookup_cache[code] = day_ohlc

                # 预排序日期列表（给 _calc_atr_from_ohlc 用，避免每根 K 线重复排序）
                sorted_ohlc_dates = sorted(day_ohlc.keys())

                atr_stop: float | None = None  # ATR 动态止损（ratchet up only）
                hard_floor = entry_close * (1.0 + atr_hard_stop_pct / 100.0)  # 极限止损地板
                use_trailing = trailing_stop_pct < 0
                trailing_activated = trailing_activate_pct <= 0
                activate_price = entry_close * (1.0 + trailing_activate_pct / 100.0) if not trailing_activated else 0.0
                peak_high = entry_close

                prev_close_atr = entry_close
                for mkt_day in market_window:
                    candle = day_ohlc.get(mkt_day)
                    if candle is None:
                        continue
                    open_px, high, low, close_px = candle

                    # 一字跌停：无法卖出，跳过（open==high==low 且较前日下跌）
                    tol = open_px * 1e-6
                    if (
                        open_px > 0
                        and abs(high - open_px) <= tol
                        and abs(low - open_px) <= tol
                        and open_px < prev_close_atr
                    ):
                        prev_close_atr = close_px
                        continue
                    prev_close_atr = close_px

                    # 1. 计算当日 ATR，更新 ATR 止损（ratchet up only）
                    atr_val = _calc_atr_from_ohlc(sorted_ohlc_dates, day_ohlc, mkt_day, atr_period)
                    if atr_val and atr_val > 0:
                        new_atr_stop = close_px - atr_multiplier * atr_val
                        if atr_stop is None:
                            atr_stop = new_atr_stop
                        else:
                            atr_stop = max(atr_stop, new_atr_stop)  # ratchet up

                    # 2. 有效止损 = max(ATR 动态止损, 极限地板)
                    effective_stop = max(atr_stop or hard_floor, hard_floor)

                    # 3. 移动止盈（激活门槛 + 百分比回撤）
                    if use_trailing and not trailing_activated and high >= activate_price:
                        trailing_activated = True
                    trailing_price = (
                        peak_high * (1.0 + trailing_stop_pct / 100.0) if use_trailing and trailing_activated else None
                    )

                    # 4. 检查触发：ATR 止损 → trailing（无固定止盈）
                    hit = False
                    if low <= effective_stop:
                        exit_close = effective_stop if open_px >= effective_stop else open_px
                        exit_date = mkt_day
                        hit = True
                    elif trailing_price is not None and low <= trailing_price:
                        exit_close = trailing_price if open_px >= trailing_price else open_px
                        exit_date = mkt_day
                        hit = True

                    if hit:
                        break

                    peak_high = max(peak_high, high)

                if exit_close is None:
                    # 安全网到期：按窗口最后一天收盘离场
                    exit_close, exit_date = _close_on_or_before(
                        full_df,
                        actual_exit_anchor,
                        lower_exclusive=signal_date,
                    )

            if exit_close is None or exit_date is None:
                continue
            entry_exec = entry_close * (1.0 + buy_friction_pct / 100.0)
            exit_exec = exit_close * (1.0 - sell_friction_pct / 100.0)
            if entry_exec <= 0:
                continue
            ret_pct = (exit_exec - entry_exec) / entry_exec * 100.0
            _, trigger_name = name_score_map.get(code, (0.0, "Layer3_Backup"))
            score = float(p_score_map.get(code, 0.0))
            records.append(
                TradeRecord(
                    signal_date=signal_date,
                    entry_date=actual_entry_date,
                    exit_date=exit_date,
                    code=code,
                    name=name_map.get(code, code),
                    trigger=trigger_name,
                    score=score,
                    entry_close=entry_close,
                    exit_close=exit_close,
                    ret_pct=ret_pct,
                    track=track_map.get(code, ""),
                    regime=regime,
                )
            )

        if (idx + 1) % 20 == 0 or (idx + 1) == max_idx:
            print(f"[backtest] 回放进度 {idx + 1}/{max_idx}, trades={len(records)}")
            report_progress("回放交易", f"{idx + 1}/{max_idx}", 0.4 + (idx + 1) / max_idx * 0.6)

    trades_df = pd.DataFrame([r.__dict__ for r in records])
    summary = {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "hold_days": hold_days,
        "top_n": top_n,
        "ai_selection_mode": FUNNEL_AI_SELECTION_MODE,
        "ai_top_n_cap": None if int(top_n) <= 0 else int(top_n),
        "board": board,
        "sample_size": sample_size,
        "trading_days": trading_days,
        "universe_ok": len(all_df_map),
        "universe_fail": len(failures),
        "snapshot_used": snapshot_used,
        "snapshot_rows_total": snapshot_rows_total,
        "eval_days": eval_days,
        "signal_days": signal_days,
        "trades": len(trades_df),
        "exit_mode": exit_mode,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "trailing_stop_pct": trailing_stop_pct,
        "trailing_activate_pct": trailing_activate_pct,
        "atr_period": atr_period if exit_mode == "atr" else None,
        "atr_multiplier": atr_multiplier if exit_mode == "atr" else None,
        "atr_hard_stop_pct": atr_hard_stop_pct if exit_mode == "atr" else None,
        "sltp_priority": sltp_priority,
        "use_current_meta": bool(use_current_meta),
        "buy_friction_pct": float(buy_friction_pct),
        "sell_friction_pct": float(sell_friction_pct),
        "regime_filter": bool(regime_filter),
        "pending_mode": pending_mode,
        "pending_merge_order": pending_merge_order,
        "pending_confirmed_total": pending_confirmed_total,
        "cache_only_first": bool(BACKTEST_CACHE_ONLY_FIRST),
        "metrics_engine": metrics_engine,
        "wbt_fee_rate": float(wbt_fee_rate),
        "wbt_n_jobs": int(wbt_n_jobs),
        "wbt_requested": metrics_engine in {"auto", "both", "wbt"},
        "wbt_available": None,
        "wbt_error": "",
    }
    if not trades_df.empty:
        ret = pd.to_numeric(trades_df["ret_pct"], errors="coerce").dropna()
        var95_ret_pct, cvar95_ret_pct = _calc_cvar95_pct(ret)

        # 组合级 NAV 曲线 → 正确的 Sharpe/MDD/Calmar
        nav_df = _build_daily_nav(
            records,
            all_df_map,
            ohlc_lookup_cache,
            trade_dates,
            start_dt,
            end_dt,
            top_n,
            buy_friction_pct,
        )
        pm = _calc_portfolio_metrics(nav_df)

        summary.update(
            {
                "win_rate_pct": float((ret > 0).mean() * 100.0),
                "avg_ret_pct": float(ret.mean()),
                "median_ret_pct": float(ret.median()),
                "q25_ret_pct": float(ret.quantile(0.25)),
                "q75_ret_pct": float(ret.quantile(0.75)),
                "max_drawdown_pct": pm.get("portfolio_mdd_pct"),
                "var95_ret_pct": var95_ret_pct,
                "cvar95_ret_pct": cvar95_ret_pct,
                "max_consecutive_losses": _calc_max_consecutive_losses(ret),
                "sharpe_ratio": pm.get("portfolio_sharpe"),
                "calmar_ratio": pm.get("portfolio_calmar"),
                "portfolio_ann_ret_pct": pm.get("portfolio_ann_ret_pct"),
                "portfolio_total_ret_pct": pm.get("portfolio_total_ret_pct"),
                "portfolio_trading_days": pm.get("portfolio_trading_days"),
                "portfolio_avg_positions": pm.get("portfolio_avg_positions"),
                "_nav_df": nav_df,
                "stratified": _calc_stratified_stats(trades_df, hold_days=hold_days),
            }
        )

        if metrics_engine in {"auto", "both", "wbt"}:
            from core.wbt_adapter import (
                build_position_weight_frame,
                evaluate_nav_with_wbt,
                wbt_summary_fields,
            )

            wbt_eval = evaluate_nav_with_wbt(
                nav_df,
                fee_rate=wbt_fee_rate,
                n_jobs=wbt_n_jobs,
                yearly_days=250,
            )
            if metrics_engine == "wbt" and not wbt_eval.available:
                raise RuntimeError(f"metrics_engine=wbt 但 wbt 不可用。请先安装 wbt，当前错误: {wbt_eval.error}")
            summary.update(wbt_summary_fields(wbt_eval))
            if wbt_eval.available:
                summary["wbt_stats"] = wbt_eval.stats or {}
                summary["wbt_long_stats"] = wbt_eval.long_stats or {}
                summary["wbt_short_stats"] = wbt_eval.short_stats or {}
                summary["_wbt_daily_return_df"] = wbt_eval.daily_return
                summary["_wbt_dailys_df"] = wbt_eval.dailys
                summary["_wbt_pairs_df"] = wbt_eval.pairs
            summary["_wbt_weight_df"] = build_position_weight_frame(
                records=records,
                all_df_map=all_df_map,
                ohlc_cache=ohlc_lookup_cache,
                trade_dates=trade_dates,
                start_dt=start_dt,
                end_dt=end_dt,
            )
    else:
        summary.update(
            {
                "win_rate_pct": None,
                "avg_ret_pct": None,
                "median_ret_pct": None,
                "q25_ret_pct": None,
                "q75_ret_pct": None,
                "max_drawdown_pct": None,
                "var95_ret_pct": None,
                "cvar95_ret_pct": None,
                "max_consecutive_losses": 0,
                "sharpe_ratio": None,
                "calmar_ratio": None,
                "portfolio_ann_ret_pct": None,
                "portfolio_total_ret_pct": None,
                "portfolio_trading_days": 0,
                "portfolio_avg_positions": 0.0,
                "stratified": {},
                "wbt_available": False if metrics_engine in {"auto", "both", "wbt"} else None,
                "wbt_error": "no trades" if metrics_engine in {"auto", "both", "wbt"} else "",
            }
        )
    return trades_df, summary


def _fmt_metric(v: float | int | str | None, ndigits: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{ndigits}f}"
    return str(v)


def _calc_max_drawdown_pct(ret: pd.Series) -> float | None:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if s.empty:
        return None
    nav = (1.0 + s / 100.0).cumprod()
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    if drawdown.empty:
        return None
    return float(drawdown.min() * 100.0)


def _calc_cvar95_pct(ret: pd.Series) -> tuple[float | None, float | None]:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if s.empty:
        return None, None
    var95 = float(s.quantile(0.05))
    tail = s[s <= var95]
    if tail.empty:
        return var95, None
    return var95, float(tail.mean())


def _calc_max_consecutive_losses(ret: pd.Series) -> int:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if s.empty:
        return 0
    max_streak = 0
    streak = 0
    for v in s.tolist():
        if float(v) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return int(max_streak)


def _calc_sharpe_ratio(
    ret: pd.Series,
    risk_free_annual: float = 2.0,
    periods_per_year: float | None = None,
    hold_days: int = DEFAULT_HOLD_DAYS,
) -> float | None:
    """
    年化夏普比 = (年化收益 - 无风险利率) / 年化波动率。
    ret: 每笔交易收益率(%)序列。
    periods_per_year: 每年可执行的交易轮次。默认根据 hold_days 推算 (250 / hold_days)。
    """
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if len(s) < 3:
        return None
    mean_pct = float(s.mean())
    std_pct = float(s.std(ddof=1))
    if std_pct <= 0:
        return None
    if periods_per_year is None:
        periods_per_year = 250.0 / max(hold_days, 1)
    ann_ret = mean_pct * periods_per_year / 100.0
    ann_std = std_pct * (periods_per_year**0.5) / 100.0
    rf = risk_free_annual / 100.0
    return float((ann_ret - rf) / ann_std)


def _calc_calmar_ratio(
    ret: pd.Series,
    periods_per_year: float | None = None,
    hold_days: int = DEFAULT_HOLD_DAYS,
) -> float | None:
    """卡玛比 = 年化收益 / abs(最大回撤)。"""
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if len(s) < 3:
        return None
    mdd = _calc_max_drawdown_pct(s)
    if mdd is None or mdd >= 0:
        return None
    if periods_per_year is None:
        periods_per_year = 250.0 / max(hold_days, 1)
    mean_pct = float(s.mean())
    ann_ret_pct = mean_pct * periods_per_year
    return float(ann_ret_pct / abs(mdd))


def _calc_information_ratio(
    ret: pd.Series,
    bench_ret: pd.Series | None,
    periods_per_year: float = 250.0,
) -> float | None:
    """信息比 = 年化超额收益 / 年化跟踪误差。"""
    if bench_ret is None:
        return None
    s = pd.to_numeric(ret, errors="coerce").dropna()
    b = pd.to_numeric(bench_ret, errors="coerce").dropna()
    n = min(len(s), len(b))
    if n < 3:
        return None
    excess = s.iloc[:n].values - b.iloc[:n].values
    excess_mean = float(excess.mean())
    excess_std = float(excess.std(ddof=1))
    if excess_std <= 0:
        return None
    ann_excess = excess_mean * periods_per_year / 100.0
    ann_te = excess_std * (periods_per_year**0.5) / 100.0
    return float(ann_excess / ann_te)


def _calc_stratified_stats(trades_df: pd.DataFrame, hold_days: int = DEFAULT_HOLD_DAYS) -> dict[str, dict]:
    """
    按 track (Trend/Accum) 和 regime 分层统计。
    返回 {"by_track": {"Trend": {...}, "Accum": {...}},
           "by_regime": {"RISK_ON": {...}, "RISK_OFF": {...}, ...}}
    """
    result: dict[str, dict] = {"by_track": {}, "by_regime": {}}
    if trades_df.empty:
        return result

    def _stats_for_slice(df_slice: pd.DataFrame) -> dict:
        ret = pd.to_numeric(df_slice.get("ret_pct"), errors="coerce").dropna()
        n = len(ret)
        if n == 0:
            return {"trades": 0}
        var95, cvar95 = _calc_cvar95_pct(ret)
        return {
            "trades": n,
            "win_rate_pct": float((ret > 0).mean() * 100.0),
            "avg_ret_pct": float(ret.mean()),
            "median_ret_pct": float(ret.median()),
            "max_drawdown_pct": _calc_max_drawdown_pct(ret),
            "sharpe_ratio": _calc_sharpe_ratio(ret, hold_days=hold_days),
            "calmar_ratio": _calc_calmar_ratio(ret, hold_days=hold_days),
            "var95_ret_pct": var95,
            "cvar95_ret_pct": cvar95,
            "max_consecutive_losses": _calc_max_consecutive_losses(ret),
        }

    # by track
    for track_val in ["Trend", "Accum"]:
        mask = trades_df["track"] == track_val
        if mask.any():
            result["by_track"][track_val] = _stats_for_slice(trades_df[mask])

    # by regime
    if "regime" in trades_df.columns:
        for regime_val in trades_df["regime"].dropna().unique():
            regime_str = str(regime_val).strip()
            if regime_str:
                mask = trades_df["regime"] == regime_str
                if mask.any():
                    result["by_regime"][regime_str] = _stats_for_slice(trades_df[mask])

    # cross: track × regime
    cross: dict[str, dict] = {}
    for track_val in ["Trend", "Accum"]:
        if "regime" not in trades_df.columns:
            break
        for regime_val in trades_df["regime"].dropna().unique():
            regime_str = str(regime_val).strip()
            mask = (trades_df["track"] == track_val) & (trades_df["regime"] == regime_str)
            if mask.any():
                key = f"{track_val}_{regime_str}"
                cross[key] = _stats_for_slice(trades_df[mask])
    if cross:
        result["by_track_regime"] = cross

    return result


# ---------------------------------------------------------------------------
# 组合级净值曲线 & 指标（替代逐笔 cumprod 的错误算法）
# ---------------------------------------------------------------------------


def _build_daily_nav(
    records: list[TradeRecord],
    all_df_map: dict[str, pd.DataFrame],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    trade_dates: list[date],
    start_dt: date,
    end_dt: date,
    top_n: int,
    buy_friction_pct: float = 0.0,
) -> pd.DataFrame:
    """
    从交易记录 + 每日 OHLCV 构建 mark-to-market 组合净值曲线。

    算法：等权归一化收益指数。
    - 每天对所有 open 持仓按收盘价 mark-to-market
    - 组合日收益 = open 持仓收益率的等权平均（无持仓日=0）
    - NAV[t] = NAV[t-1] × (1 + portfolio_daily_ret)
    """
    if not records:
        return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "positions_count"])

    # 为每笔交易建立持仓信息
    # entry_date = signal 次日（实际买入日），exit_date = 实际卖出日
    # entry_exec = entry_close × (1 + friction)，与 ret_pct 口径一致
    positions: list[dict] = []
    for r in records:
        entry_date = r.entry_date
        if entry_date is None:
            # 兼容旧 trades：entry_target = signal_date 的下一个交易日。
            try:
                sig_idx = next(i for i, d in enumerate(trade_dates) if d >= r.signal_date)
                entry_date = trade_dates[sig_idx + 1] if sig_idx + 1 < len(trade_dates) else None
            except StopIteration:
                entry_date = None
        if entry_date is None:
            continue
        entry_exec = r.entry_close * (1.0 + buy_friction_pct / 100.0)
        if entry_exec <= 0:
            continue
        # 确保 ohlc_cache 有此 code
        if r.code not in ohlc_cache:
            df = all_df_map.get(r.code)
            if df is not None and not df.empty:
                ohlc_cache[r.code] = _build_daily_ohlc_lookup(df)
        positions.append(
            {
                "code": r.code,
                "entry_date": entry_date,
                "exit_date": r.exit_date,
                "entry_exec": entry_exec,
            }
        )

    if not positions:
        return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "positions_count"])

    window = [d for d in trade_dates if start_dt <= d <= end_dt]
    if not window:
        return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "positions_count"])

    nav = 1.0
    prev_mtm: dict[int, float] = {}  # position_idx -> 昨日 mtm 价格
    rows: list[dict] = []

    for day in window:
        open_indices: list[int] = []
        daily_rets: list[float] = []

        for idx, pos in enumerate(positions):
            if pos["entry_date"] > day or pos["exit_date"] < day:
                continue
            open_indices.append(idx)

            ohlc = ohlc_cache.get(pos["code"], {})
            candle = ohlc.get(day)
            if candle is None:
                # 无此日行情（停牌），沿用昨日 mtm
                daily_rets.append(0.0)
                continue

            close_today = candle[3]  # (open, high, low, close)
            prev_price = prev_mtm.get(idx, pos["entry_exec"])
            if prev_price > 0:
                daily_rets.append(close_today / prev_price - 1.0)
            else:
                daily_rets.append(0.0)
            prev_mtm[idx] = close_today

        n_open = len(open_indices)
        if n_open > 0 and daily_rets:
            port_ret = sum(daily_rets) / n_open
        else:
            port_ret = 0.0

        nav *= 1.0 + port_ret
        rows.append(
            {
                "date": day,
                "nav": nav,
                "daily_ret_pct": port_ret * 100.0,
                "positions_count": n_open,
            }
        )

        # 清理已结束持仓的 prev_mtm
        for idx in list(prev_mtm.keys()):
            if positions[idx]["exit_date"] < day:
                del prev_mtm[idx]

    return pd.DataFrame(rows)


def _calc_portfolio_metrics(
    nav_df: pd.DataFrame,
    risk_free_annual: float = 2.0,
) -> dict:
    """从每日 NAV 曲线计算组合级风险调整指标。"""
    empty = {
        "portfolio_sharpe": None,
        "portfolio_mdd_pct": None,
        "portfolio_calmar": None,
        "portfolio_ann_ret_pct": None,
        "portfolio_total_ret_pct": None,
        "portfolio_trading_days": 0,
        "portfolio_avg_positions": 0.0,
    }
    if nav_df is None or nav_df.empty or len(nav_df) < 2:
        return empty

    nav = nav_df["nav"]
    daily_ret = nav_df["daily_ret_pct"] / 100.0  # 转为小数

    n_days = len(nav_df)
    total_ret_pct = (float(nav.iloc[-1]) / float(nav.iloc[0]) - 1.0) * 100.0
    ann_factor = 250.0 / max(n_days, 1)
    ann_ret_pct = total_ret_pct * ann_factor

    # MDD
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    mdd_pct = float(drawdown.min()) * 100.0

    # Sharpe
    rf_daily = risk_free_annual / 100.0 / 250.0
    excess = daily_ret - rf_daily
    std_daily = float(excess.std(ddof=1))
    if std_daily > 0 and len(excess) >= 3:
        sharpe = float(excess.mean()) / std_daily * (250.0**0.5)
    else:
        sharpe = None

    # Calmar
    if mdd_pct < 0:
        calmar = ann_ret_pct / abs(mdd_pct)
    else:
        calmar = None

    avg_pos = float(nav_df["positions_count"].mean()) if "positions_count" in nav_df.columns else 0.0

    return {
        "portfolio_sharpe": sharpe,
        "portfolio_mdd_pct": mdd_pct,
        "portfolio_calmar": calmar,
        "portfolio_ann_ret_pct": ann_ret_pct,
        "portfolio_total_ret_pct": total_ret_pct,
        "portfolio_trading_days": n_days,
        "portfolio_avg_positions": avg_pos,
    }


# ---------------------------------------------------------------------------
# 策略建议自动生成
# ---------------------------------------------------------------------------


def _generate_strategy_advice(summary: dict) -> list[str]:
    """根据回测分层统计自动生成策略调整建议。"""
    advice: list[str] = []

    win_rate = summary.get("win_rate_pct")
    summary.get("avg_ret_pct")
    mdd = summary.get("max_drawdown_pct")
    sharpe = summary.get("sharpe_ratio")
    max_consec = summary.get("max_consecutive_losses", 0)
    avg_pos = summary.get("portfolio_avg_positions", 0)
    summary.get("hold_days", 0)
    summary.get("stop_loss_pct", 0)
    take_profit = summary.get("take_profit_pct", 0)
    stratified = summary.get("stratified", {})
    by_regime = stratified.get("by_regime", {})
    by_track = stratified.get("by_track", {})

    # 1. 各水温环境诊断
    for regime, stats in sorted(by_regime.items()):
        r_avg = stats.get("avg_ret_pct")
        r_trades = stats.get("trades", 0)
        stats.get("win_rate_pct")
        if r_avg is not None and r_trades >= 10 and r_avg < -1.5:
            advice.append(f"🔴 {regime} 环境下平均收益 {r_avg:+.2f}%（{r_trades}笔），建议该水温下暂停开仓或大幅降仓")
        elif r_avg is not None and r_trades >= 10 and r_avg < -0.5:
            advice.append(f"🟡 {regime} 环境下平均收益 {r_avg:+.2f}%（{r_trades}笔），建议降低仓位至 30% 以下")
        elif r_avg is not None and r_trades >= 10 and r_avg > 1.0:
            advice.append(f"🟢 {regime} 环境下表现较好（均收 {r_avg:+.2f}%），可加大仓位")

    # 2. Trend vs Accum 分化
    t_stats = by_track.get("Trend", {})
    a_stats = by_track.get("Accum", {})
    t_sharpe = t_stats.get("sharpe_ratio")
    a_sharpe = a_stats.get("sharpe_ratio")
    if t_sharpe is not None and a_sharpe is not None:
        diff = abs((t_sharpe or 0) - (a_sharpe or 0))
        if diff > 0.5:
            better = "Accum" if (a_sharpe or 0) > (t_sharpe or 0) else "Trend"
            worse = "Trend" if better == "Accum" else "Accum"
            advice.append(
                f"🟡 {better}（夏普 {by_track[better].get('sharpe_ratio', 0):.3f}）"
                f"明显优于 {worse}（夏普 {by_track[worse].get('sharpe_ratio', 0):.3f}），"
                f"考虑侧重 {better} 信号"
            )

    # 3. 整体胜率
    if win_rate is not None and win_rate < 35:
        advice.append(f"🔴 整体胜率仅 {win_rate:.1f}%，低于 35% 警戒线，建议收紧入场筛选条件或增加信号确认环节")
    elif win_rate is not None and win_rate < 45:
        advice.append(f"🟡 胜率 {win_rate:.1f}%，偏低，考虑提高信号分数门槛")

    # 4. 回撤
    if mdd is not None and mdd < -25:
        advice.append(f"🔴 最大回撤 {mdd:.1f}%，建议收紧止损线或降低每日候选数 TopN")
    elif mdd is not None and mdd < -15:
        advice.append(f"🟡 最大回撤 {mdd:.1f}%，关注风控参数是否偏松")

    # 5. 连续亏损
    if max_consec and int(max_consec) >= 8:
        advice.append(f"🔴 最长连续亏损 {int(max_consec)} 笔，建议增加信号确认机制或缩短持有期")
    elif max_consec and int(max_consec) >= 5:
        advice.append(f"🟡 最长连续亏损 {int(max_consec)} 笔，关注是否需要加入熔断机制")

    # 6. 持仓稀疏
    if avg_pos is not None and avg_pos < 0.5:
        advice.append("🟡 大部分交易日无持仓，信号触发过少，考虑放宽筛选条件或扩大股票池")

    # 7. 止盈效果（如果开了止盈但夏普仍负）
    if take_profit and take_profit > 0 and sharpe is not None and sharpe < -0.3:
        advice.append(f"🟡 开启 TP{take_profit:.0f}% 后夏普仍为 {sharpe:.3f}，止盈可能过早截断盈利单，建议尝试关闭止盈")

    # 8. 夏普整体评估
    if sharpe is not None and sharpe > 0.5:
        advice.append(f"🟢 组合夏普 {sharpe:.3f}，策略表现良好")
    elif sharpe is not None and sharpe < -0.5:
        advice.append(f"🔴 组合夏普 {sharpe:.3f}，策略整体亏损，需要全面复盘信号源质量")

    if not advice:
        advice.append("🟢 当前参数组合表现尚可，暂无强烈调整建议")

    return advice


def _build_summary_md(summary: dict) -> str:
    use_current_meta = bool(summary.get("use_current_meta"))
    meta_mode = (
        "current_snapshot (⚠️ look-ahead bias)"
        if use_current_meta
        else "disabled_current_snapshot_filters (bias-reduced)"
    )
    notes = [
        "- 该回测使用日线数据（qfq），含 T+1 与涨跌停成交约束（一字板不可成交）。",
        "- 入场口径：信号日收盘后出信号，次日开盘价买入（跳过一字涨停日）。",
        "- 已纳入双边交易摩擦成本（买入/卖出各0.5%），用于近似滑点 + 佣金 + 税费影响。",
        "- ⚠️ 仍存在幸存者偏差：股票池来自当前在市样本，未包含历史退市股票。",
    ]
    if use_current_meta:
        notes.append(
            "- ⚠️ 市值/行业映射采用当前截面，会引入 look-ahead bias （市值穿越与行业漂移）；该结果仅用于参数方向验证。"
        )
    else:
        notes.append("- 本次已关闭当前截面市值/行业映射过滤（Layer1 市值 + Layer3 行业共振），用于降低前视偏差。")
    if summary.get("wbt_requested"):
        notes.append(
            "- wbt 为 MIT License 的可选权重回测后端；当前实现不 vendoring 其源码，仅在本机/CI 已安装 wbt 时导入使用。"
        )
        notes.append(
            "- wbt 辅助指标基于 legacy NAV 的合成权重序列，主要用于高性能统计与报告交叉校验；"
            "交易执行真值仍以本回测器的 T+1/止损/止盈/涨跌停回放为准。"
        )
    lines = [
        "# Wyckoff Funnel Daily Backtest",
        "",
        f"- 区间: {summary.get('start')} ~ {summary.get('end')}",
        f"- 持有周期: {summary.get('hold_days')} 交易日",
        (
            f"- 每日候选上限: Top {summary.get('top_n')}"
            if summary.get("ai_top_n_cap") is not None
            else "- 每日候选上限: 不限（回测全量 AI 输入）"
        ),
        f"- AI 候选模式: {summary.get('ai_selection_mode')}",
        f"- 股票池: {summary.get('board')} (sample={summary.get('sample_size')})",
        f"- 评估交易日: {summary.get('eval_days')}",
        f"- 触发交易日: {summary.get('signal_days')}",
        f"- 离场模式: {summary.get('exit_mode')}",
        *(
            [
                f"- ATR 周期: {summary.get('atr_period')}",
                f"- ATR 乘数: {summary.get('atr_multiplier')}",
                f"- ATR 极限止损: {_fmt_metric(summary.get('atr_hard_stop_pct'), 1)}%",
                f"- 最大持有天数: {DEFAULT_ATR_MAX_HOLD_DAYS}（安全网）",
            ]
            if summary.get("exit_mode") == "atr"
            else [
                f"- 止损线: {_fmt_metric(summary.get('stop_loss_pct'), 1)}%",
                f"- 止盈线: {_fmt_metric(summary.get('take_profit_pct'), 1)}%",
            ]
        ),
        f"- 移动止盈: {_fmt_metric(summary.get('trailing_stop_pct'), 1)}%（从最高点回撤，浮盈≥{_fmt_metric(summary.get('trailing_activate_pct'), 1)}%后激活）"
        if summary.get("trailing_stop_pct", 0) < 0
        else "- 移动止盈: 关闭",
        f"- 日内触发优先级: {summary.get('sltp_priority')}",
        f"- 买入摩擦成本: {_fmt_metric(summary.get('buy_friction_pct'), 3)}%",
        f"- 卖出摩擦成本: {_fmt_metric(summary.get('sell_friction_pct'), 3)}%",
        f"- 元数据口径: {meta_mode}",
        f"- 信号确认模式: {summary.get('pending_mode')}",
        f"- 大盘水温仓控: {'开启' if summary.get('regime_filter') else '关闭'}",
        f"- 绩效引擎: {summary.get('metrics_engine', 'legacy')}"
        + (
            "（wbt 可用）"
            if summary.get("wbt_available") is True
            else ("（wbt 未启用）" if not summary.get("wbt_requested") else "（wbt 不可用，已保留 legacy 指标）")
        ),
        f"- 成交样本: {summary.get('trades')}",
        "",
        "## 收益统计",
        f"- 胜率: {_fmt_metric(summary.get('win_rate_pct'), 2)}%",
        f"- 平均收益: {_fmt_metric(summary.get('avg_ret_pct'), 3)}%",
        f"- 中位收益: {_fmt_metric(summary.get('median_ret_pct'), 3)}%",
        f"- 25%分位: {_fmt_metric(summary.get('q25_ret_pct'), 3)}%",
        f"- 75%分位: {_fmt_metric(summary.get('q75_ret_pct'), 3)}%",
        "",
        "## 组合风险指标（基于每日净值曲线）",
        f"- 夏普比 (Sharpe Ratio): {_fmt_metric(summary.get('sharpe_ratio'), 3)}",
        f"- 卡玛比 (Calmar Ratio): {_fmt_metric(summary.get('calmar_ratio'), 3)}",
        f"- 最大回撤: {_fmt_metric(summary.get('max_drawdown_pct'), 2)}%",
        f"- 组合年化收益: {_fmt_metric(summary.get('portfolio_ann_ret_pct'), 2)}%",
        f"- 组合总收益: {_fmt_metric(summary.get('portfolio_total_ret_pct'), 2)}%",
        f"- 平均持仓数: {_fmt_metric(summary.get('portfolio_avg_positions'), 1)}",
        "",
        *(
            [
                "## wbt 权重回测辅助指标",
                f"- wbt 年化收益: {_fmt_metric(summary.get('wbt_ann_return_pct'), 2)}%",
                f"- wbt 绝对收益: {_fmt_metric(summary.get('wbt_abs_return_pct'), 2)}%",
                f"- wbt 夏普比: {_fmt_metric(summary.get('wbt_sharpe_ratio'), 3)}",
                f"- wbt 卡玛比: {_fmt_metric(summary.get('wbt_calmar_ratio'), 3)}",
                f"- wbt 最大回撤: {_fmt_metric(summary.get('wbt_max_drawdown_pct'), 2)}%",
                f"- wbt 日胜率: {_fmt_metric(summary.get('wbt_daily_win_rate_pct'), 2)}%",
                "",
            ]
            if summary.get("wbt_available") is True
            else (
                [
                    "## wbt 权重回测辅助指标",
                    f"- 状态: 不可用（{summary.get('wbt_error') or '未安装 wbt'}）",
                    "",
                ]
                if summary.get("wbt_requested")
                else []
            )
        ),
        "## 逐笔风险统计",
        f"- VaR95(单笔收益): {_fmt_metric(summary.get('var95_ret_pct'), 3)}%",
        f"- CVaR95(最差5%均值): {_fmt_metric(summary.get('cvar95_ret_pct'), 3)}%",
        f"- 最长连续亏损笔数: {_fmt_metric(summary.get('max_consecutive_losses'), 0)}",
    ]

    # Stratified stats tables
    stratified = summary.get("stratified", {})
    by_track = stratified.get("by_track", {})
    if by_track:
        lines.extend(["", "## 分层统计：Trend vs Accum", ""])
        lines.append("| 指标 | Trend | Accum |")
        lines.append("|------|-------|-------|")
        metrics_labels = [
            ("trades", "成交笔数", 0),
            ("win_rate_pct", "胜率(%)", 2),
            ("avg_ret_pct", "平均收益(%)", 3),
            ("median_ret_pct", "中位收益(%)", 3),
            ("max_drawdown_pct", "最大回撤(%)", 3),
            ("sharpe_ratio", "夏普比", 3),
            ("calmar_ratio", "卡玛比", 3),
            ("max_consecutive_losses", "最长连亏", 0),
        ]
        for key, label, nd in metrics_labels:
            t_val = by_track.get("Trend", {}).get(key)
            a_val = by_track.get("Accum", {}).get(key)
            lines.append(f"| {label} | {_fmt_metric(t_val, nd)} | {_fmt_metric(a_val, nd)} |")

    by_regime = stratified.get("by_regime", {})
    if by_regime:
        lines.extend(["", "## 分层统计：按大盘水温", ""])
        regime_keys = sorted(by_regime.keys())
        header = "| 指标 | " + " | ".join(regime_keys) + " |"
        sep = "|------|" + "|".join(["-------"] * len(regime_keys)) + "|"
        lines.append(header)
        lines.append(sep)
        for key, label, nd in [
            ("trades", "成交笔数", 0),
            ("win_rate_pct", "胜率(%)", 2),
            ("avg_ret_pct", "平均收益(%)", 3),
            ("sharpe_ratio", "夏普比", 3),
        ]:
            vals = [_fmt_metric(by_regime[rk].get(key), nd) for rk in regime_keys]
            lines.append(f"| {label} | " + " | ".join(vals) + " |")

    # 策略调整建议
    advice_items = _generate_strategy_advice(summary)
    if advice_items:
        lines.extend(["", "## 策略调整建议", ""])
        for i, item in enumerate(advice_items, 1):
            lines.append(f"{i}. {item}")

    lines.extend(["", "## 说明", *notes])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wyckoff Funnel 日线轻量回测器")
    _default_end = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    _default_start = (date.today() - timedelta(days=548)).strftime("%Y-%m-%d")  # ~18 个月
    parser.add_argument("--start", default=_default_start, help=f"起始日期 (default: {_default_start}，约18个月前)")
    parser.add_argument("--end", default=_default_end, help=f"结束日期 (default: {_default_end}，T-1)")
    parser.add_argument(
        "--hold-days",
        type=int,
        default=DEFAULT_HOLD_DAYS,
        help=f"持有交易日数 (default: {DEFAULT_HOLD_DAYS})",
    )
    parser.add_argument(
        "--hold-days-list",
        default="",
        help="逗号分隔的持有周期列表，例如 10,15,20,30。设置后会依次回测并输出汇总。",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=0,
        help="每日候选上限；0 表示不截断（回测全量 AI 输入，默认 0）",
    )
    parser.add_argument(
        "--board",
        choices=["main_chinext", "all", "main", "chinext"],
        default="main_chinext",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=0,
        help="股票池采样数量；0 表示不采样（默认全量，贴近线上）",
    )
    parser.add_argument("--trading-days", type=int, default=320, help="单次筛选回看交易日数")
    parser.add_argument("--workers", type=int, default=8, help="历史拉取并发数")
    parser.add_argument(
        "--exit-mode",
        choices=["close_only", "sltp", "atr"],
        default=DEFAULT_EXIT_MODE,
        help=f"离场模式：close_only=收盘离场；sltp=固定止盈止损；atr=ATR动态止损(对齐实盘) (default: {DEFAULT_EXIT_MODE})",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=DEFAULT_STOP_LOSS_PCT,
        help=f"止损线(%%), 如 -9.0 表示跌破 9%% 止损. 0 表示不设止损 (default: {DEFAULT_STOP_LOSS_PCT})",
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=DEFAULT_TAKE_PROFIT_PCT,
        help=f"止盈线(%%), 如 10.0 表示涨超 10%% 止盈. 0 表示不设止盈 (default: {DEFAULT_TAKE_PROFIT_PCT})",
    )
    parser.add_argument(
        "--trailing-stop",
        type=float,
        default=DEFAULT_TRAILING_STOP_PCT,
        help=f"移动止盈(%%), 如 -5.0 表示从最高点回撤 5%% 卖出. 0 表示不启用 (default: {DEFAULT_TRAILING_STOP_PCT})",
    )
    parser.add_argument(
        "--trailing-activate",
        type=float,
        default=DEFAULT_TRAILING_ACTIVATE_PCT,
        help=f"移动止盈激活门槛(%%), 浮盈达到此值后才启用移动止盈. 0 表示立即启用 (default: {DEFAULT_TRAILING_ACTIVATE_PCT})",
    )
    parser.add_argument(
        "--atr-period",
        type=int,
        default=DEFAULT_ATR_PERIOD,
        help=f"ATR 周期（仅 atr 模式生效） (default: {DEFAULT_ATR_PERIOD})",
    )
    parser.add_argument(
        "--atr-multiplier",
        type=float,
        default=DEFAULT_ATR_MULTIPLIER,
        help=f"ATR 乘数（仅 atr 模式生效，实盘=2.0） (default: {DEFAULT_ATR_MULTIPLIER})",
    )
    parser.add_argument(
        "--atr-hard-stop",
        type=float,
        default=DEFAULT_ATR_HARD_STOP_PCT,
        help=f"ATR 模式极限止损地板(%%)（仅 atr 模式生效） (default: {DEFAULT_ATR_HARD_STOP_PCT})",
    )
    parser.add_argument(
        "--sltp-priority",
        choices=["stop_first", "take_first"],
        default="stop_first",
        help="同一交易日同时触及止损/止盈时的判定顺序",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="",
        help="CI 专用：GitHub Actions Phase 1 导出的快照目录（留空则从 Supabase 缓存取数）",
    )
    parser.add_argument(
        "--output-dir",
        default="analysis/backtest",
        help="输出目录（会写 summary.md 与 trades.csv）",
    )
    parser.add_argument(
        "--use-current-meta",
        dest="use_current_meta",
        action="store_true",
        default=True,
        help="使用当前截面市值/行业映射过滤（默认开启，贴近线上）",
    )
    parser.add_argument(
        "--no-use-current-meta",
        dest="use_current_meta",
        action="store_false",
        help="关闭当前截面市值/行业映射过滤（降低 look-ahead bias）",
    )
    parser.add_argument(
        "--buy-friction-pct",
        type=float,
        default=DEFAULT_BUY_FRICTION_PCT,
        help=f"买入端摩擦成本(%%): 滑点+手续费近似 (default: {DEFAULT_BUY_FRICTION_PCT})",
    )
    parser.add_argument(
        "--sell-friction-pct",
        type=float,
        default=DEFAULT_SELL_FRICTION_PCT,
        help=f"卖出端摩擦成本(%%): 滑点+手续费+税费近似 (default: {DEFAULT_SELL_FRICTION_PCT})",
    )
    parser.add_argument(
        "--regime-filter",
        action="store_true",
        default=False,
        help="启用大盘水温仓位控制: CRASH 不开仓, RISK_ON/PANIC_REPAIR 半仓, NEUTRAL 全仓",
    )
    parser.add_argument(
        "--pending-mode",
        choices=["off", "only", "both"],
        default="both",
        help="信号确认模式: off=直接用L4信号, only=仅用确认后信号, both=两者合并(默认, 与生产链路对齐)",
    )
    parser.add_argument(
        "--pending-merge-order",
        choices=["funnel_first", "confirmed_first"],
        default="funnel_first",
        help="pending_mode=both 时合并顺序：funnel_first=Step2在前(对齐生产)，confirmed_first=确认池在前(旧口径)",
    )
    parser.add_argument(
        "--metrics-engine",
        choices=["legacy", "auto", "both", "wbt"],
        default=DEFAULT_METRICS_ENGINE if DEFAULT_METRICS_ENGINE in {"legacy", "auto", "both", "wbt"} else "legacy",
        help="绩效统计引擎：legacy=当前Python口径；auto/both=可用时附加wbt；wbt=强制要求wbt可用",
    )
    parser.add_argument(
        "--wbt-fee-rate",
        type=float,
        default=DEFAULT_WBT_FEE_RATE,
        help="wbt 合成 NAV 评估的费率；legacy NAV 已含交易摩擦，默认 0",
    )
    parser.add_argument(
        "--wbt-n-jobs",
        type=int,
        default=DEFAULT_WBT_N_JOBS,
        help="wbt Rust 后端并行线程数",
    )
    args = parser.parse_args()

    start_dt = _parse_date(args.start)
    end_dt = _parse_date(args.end)
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    hold_days_list = (
        _parse_hold_days_list(args.hold_days_list) if str(args.hold_days_list).strip() else [int(args.hold_days)]
    )

    suite_rows: list[dict] = []
    success_count = 0
    last_error: Exception | None = None
    for hold_days in hold_days_list:
        try:
            trades_df, summary = run_backtest(
                start_dt=start_dt,
                end_dt=end_dt,
                hold_days=hold_days,
                top_n=args.top_n,
                board=args.board,
                sample_size=args.sample_size,
                trading_days=args.trading_days,
                max_workers=args.workers,
                snapshot_dir=Path(args.snapshot_dir).resolve() if str(args.snapshot_dir).strip() else None,
                exit_mode=args.exit_mode,
                stop_loss_pct=args.stop_loss,
                take_profit_pct=args.take_profit,
                trailing_stop_pct=args.trailing_stop,
                trailing_activate_pct=args.trailing_activate,
                sltp_priority=args.sltp_priority,
                use_current_meta=args.use_current_meta,
                buy_friction_pct=args.buy_friction_pct,
                sell_friction_pct=args.sell_friction_pct,
                regime_filter=args.regime_filter,
                pending_mode=args.pending_mode,
                pending_merge_order=args.pending_merge_order,
                atr_period=args.atr_period,
                atr_multiplier=args.atr_multiplier,
                atr_hard_stop_pct=args.atr_hard_stop,
                metrics_engine=args.metrics_engine,
                wbt_fee_rate=args.wbt_fee_rate,
                wbt_n_jobs=args.wbt_n_jobs,
            )
        except Exception as exc:
            last_error = exc
            err_msg = str(exc)
            print(f"[backtest] hold_days={hold_days} 失败: {err_msg}")
            suite_rows.append(
                {
                    "hold_days": hold_days,
                    "trades": None,
                    "win_rate_pct": None,
                    "avg_ret_pct": None,
                    "median_ret_pct": None,
                    "max_drawdown_pct": None,
                    "sharpe_ratio": None,
                    "error": err_msg,
                }
            )
            continue

        stamp = f"{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}_h{hold_days}_n{args.top_n}"
        summary_path = out_dir / f"summary_{stamp}.md"
        trades_path = out_dir / f"trades_{stamp}.csv"

        summary_md = _build_summary_md(summary)
        summary_path.write_text(summary_md + "\n", encoding="utf-8")
        trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")

        # 输出 NAV 曲线 CSV（便于画图分析）
        nav_df = summary.pop("_nav_df", None)
        if nav_df is not None and not nav_df.empty:
            nav_path = out_dir / f"nav_{stamp}.csv"
            nav_df.to_csv(nav_path, index=False, encoding="utf-8-sig")
            print(f"[backtest] nav     -> {nav_path}")

        wbt_weight_df = summary.pop("_wbt_weight_df", None)
        if wbt_weight_df is not None and not wbt_weight_df.empty:
            wbt_weight_path = out_dir / f"wbt_weights_{stamp}.csv"
            wbt_weight_df.to_csv(wbt_weight_path, index=False, encoding="utf-8-sig")
            print(f"[backtest] wbt weights -> {wbt_weight_path}")

        wbt_daily_return_df = summary.pop("_wbt_daily_return_df", None)
        if wbt_daily_return_df is not None and not wbt_daily_return_df.empty:
            wbt_daily_path = out_dir / f"wbt_daily_return_{stamp}.csv"
            wbt_daily_return_df.to_csv(wbt_daily_path, index=False, encoding="utf-8-sig")
            print(f"[backtest] wbt daily -> {wbt_daily_path}")

        wbt_dailys_df = summary.pop("_wbt_dailys_df", None)
        if wbt_dailys_df is not None and not wbt_dailys_df.empty:
            wbt_dailys_path = out_dir / f"wbt_dailys_{stamp}.csv"
            wbt_dailys_df.to_csv(wbt_dailys_path, index=False, encoding="utf-8-sig")
            print(f"[backtest] wbt dailys -> {wbt_dailys_path}")

        wbt_pairs_df = summary.pop("_wbt_pairs_df", None)
        if wbt_pairs_df is not None and not wbt_pairs_df.empty:
            wbt_pairs_path = out_dir / f"wbt_pairs_{stamp}.csv"
            wbt_pairs_df.to_csv(wbt_pairs_path, index=False, encoding="utf-8-sig")
            print(f"[backtest] wbt pairs -> {wbt_pairs_path}")

        print(summary_md)
        print("")
        print(f"[backtest] summary -> {summary_path}")
        print(f"[backtest] trades  -> {trades_path}")
        success_count += 1

        suite_rows.append(
            {
                "hold_days": hold_days,
                "trades": summary.get("trades"),
                "win_rate_pct": summary.get("win_rate_pct"),
                "avg_ret_pct": summary.get("avg_ret_pct"),
                "median_ret_pct": summary.get("median_ret_pct"),
                "max_drawdown_pct": summary.get("max_drawdown_pct"),
                "sharpe_ratio": summary.get("sharpe_ratio"),
                "error": "",
            }
        )

    if success_count == 0:
        raise RuntimeError("多周期回测全部失败，请检查日期区间、快照覆盖范围或 TUSHARE_TOKEN。") from last_error

    if len(suite_rows) > 1:
        suite_df = pd.DataFrame(suite_rows).sort_values("hold_days").reset_index(drop=True)
        suite_stamp = f"{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
        suite_csv = out_dir / f"suite_{suite_stamp}.csv"
        suite_md = out_dir / f"suite_{suite_stamp}.md"
        suite_df.to_csv(suite_csv, index=False, encoding="utf-8-sig")

        md_lines = [
            "# AI 输入候选多周期回测汇总",
            "",
            f"- 区间: {start_dt.isoformat()} ~ {end_dt.isoformat()}",
            f"- 候选池: 送给 AI 的股票（mode={FUNNEL_AI_SELECTION_MODE}）",
            f"- 持有周期: {', '.join(str(x['hold_days']) for x in suite_rows)}",
            f"- 成功周期数: {success_count}/{len(suite_rows)}",
            "",
            "| 持有天数 | 成交笔数 | 胜率(%) | 平均收益(%) | 中位收益(%) | 最大回撤(%) | 夏普比 | 备注 |",
            "|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in suite_df.to_dict(orient="records"):
            md_lines.append(
                f"| {int(row.get('hold_days', 0))} | "
                f"{_fmt_metric(row.get('trades'), 0)} | "
                f"{_fmt_metric(row.get('win_rate_pct'), 2)} | "
                f"{_fmt_metric(row.get('avg_ret_pct'), 3)} | "
                f"{_fmt_metric(row.get('median_ret_pct'), 3)} | "
                f"{_fmt_metric(row.get('max_drawdown_pct'), 3)} | "
                f"{_fmt_metric(row.get('sharpe_ratio'), 3)} | "
                f"{str(row.get('error', '') or '').replace('|', '/')} |"
            )
        suite_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        print(f"[backtest] suite summary -> {suite_md}")
        print(f"[backtest] suite csv     -> {suite_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
