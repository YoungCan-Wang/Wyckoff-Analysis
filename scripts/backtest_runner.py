# -*- coding: utf-8 -*-
"""
日线级轻量回测器（低成本数据版）

目标：
1) 复用当前 Wyckoff Funnel 规则，不依赖分钟级或付费 Level-2 数据。
2) 在给定历史区间内，统计信号后 N 交易日收益分布与胜率。
3) 输出 summary markdown + trades csv，便于后续参数复盘。

重要说明：
- 默认关闭“当前截面市值/行业映射”过滤，以降低 look-ahead bias。
- 仍存在幸存者偏差（股票池基于当前在市样本），结果用于参数对比而非绝对收益承诺。
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wyckoff_engine import (
    FunnelConfig,
    normalize_hist_from_fetch,
    run_funnel,
    allocate_ai_candidates,
    FunnelResult,
)
from core.sector_rotation import analyze_sector_rotation
from integrations.data_source import fetch_index_hist, fetch_market_cap_map, fetch_sector_map, fetch_stock_hist
from integrations.fetch_a_share_csv import get_stocks_by_board, _normalize_symbols
from core.funnel_pipeline import (
    analyze_benchmark_and_tune_cfg as _tune_cfg_by_regime,
    calc_market_breadth as _calc_market_breadth_for_regime,
    rank_l3_candidates,
)

DEFAULT_HOLD_DAYS = 30  # 网格优化：30天夏普2.493 > 25天1.967 > 20天1.413
DEFAULT_EXIT_MODE = "sltp"
DEFAULT_STOP_LOSS_PCT = -7.0   # 网格优化最佳：SL7/TP18（夏普1.928 > SL6/TP15的1.679 > SL8/TP20的1.466）
DEFAULT_TAKE_PROFIT_PCT = 18.0
DEFAULT_USE_CURRENT_META = False
DEFAULT_BUY_FRICTION_PCT = float(os.getenv("BACKTEST_BUY_FRICTION_PCT", "0.5"))
DEFAULT_SELL_FRICTION_PCT = float(os.getenv("BACKTEST_SELL_FRICTION_PCT", "0.5"))
FUNNEL_AI_SELECTION_MODE = (
    os.getenv("FUNNEL_AI_SELECTION_MODE", "legacy_full_hits").strip().lower()
)
_LEGACY_SELECTION_MODES = {
    "legacy_full_hits",
    "legacy_hits",
    "all_hits",
    "classic",
}


@dataclass
class TradeRecord:
    signal_date: date
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


def _build_universe(board: str, sample_size: int) -> tuple[list[str], dict[str, str]]:
    if board == "main":
        items = get_stocks_by_board("main")
    elif board == "chinext":
        items = get_stocks_by_board("chinext")
    else:
        merged: dict[str, str] = {}
        for item in get_stocks_by_board("main") + get_stocks_by_board("chinext"):
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            if code not in merged:
                merged[code] = str(item.get("name", "")).strip()
        items = [{"code": c, "name": n} for c, n in merged.items()]

    name_map = {
        str(x.get("code", "")).strip(): str(x.get("name", "")).strip()
        for x in items
        if str(x.get("code", "")).strip()
    }
    # 过滤 ST 后采样（可复现）
    symbols = [
        s
        for s in _normalize_symbols(list(name_map.keys()))
        if "ST" not in name_map.get(s, "").upper()
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

    keep_cols = [c for c in ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg"] if c in df.columns]
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


def _apply_funnel_cfg_overrides(cfg: FunnelConfig) -> None:
    """
    与生产漏斗同口径：读取 FUNNEL_CFG_* 环境变量覆盖 FunnelConfig。
    """
    for f in dataclass_fields(FunnelConfig):
        key = f"FUNNEL_CFG_{f.name.upper()}"
        raw = os.getenv(key)
        if raw is None:
            continue
        val = str(raw).strip()
        if not val:
            continue
        try:
            current = getattr(cfg, f.name, None)
            if isinstance(current, bool):
                parsed = val.lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int) and not isinstance(current, bool):
                parsed = int(float(val))
            elif isinstance(current, float):
                parsed = float(val)
            else:
                parsed = val
            setattr(cfg, f.name, parsed)
        except Exception:
            # 回测不中断，保留原值
            pass


def _fetch_hist_norm(
    symbol: str,
    start_dt: date,
    end_dt: date,
) -> tuple[str, pd.DataFrame | None, str | None]:
    try:
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
    hit_score_map = {
        code: float(v[0]) for code, v in merged_trigger_map.items()
    }
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
    track_map = {c: "Trend" for c in trend_sel}
    track_map.update({c: "Accum" for c in accum_sel})
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


def _open_on_or_after(df: pd.DataFrame, d: date) -> tuple[float | None, date | None]:
    """取目标日期（含）之后首个交易日的开盘价，用于模拟次日开盘买入。"""
    row = df[df["date"] >= d].head(1)
    if row.empty:
        return None, None
    if "open" in row.columns:
        v = pd.to_numeric(row["open"], errors="coerce").dropna()
        if not v.empty:
            return float(v.iloc[0]), row.iloc[0]["date"]
    # fallback: 没有 open 列时用 close
    v = pd.to_numeric(row["close"], errors="coerce").dropna()
    if v.empty:
        return None, None
    return float(v.iloc[0]), row.iloc[0]["date"]


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
        open_v = (
            float(row.open)
            if hasattr(row, "open") and pd.notna(row.open)
            else close_v
        )
        high_v = (
            float(row.high)
            if hasattr(row, "high") and pd.notna(row.high)
            else max(open_v, close_v)
        )
        low_v = (
            float(row.low)
            if hasattr(row, "low") and pd.notna(row.low)
            else min(open_v, close_v)
        )
        out[d] = (open_v, high_v, low_v, close_v)
    return out


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
    sltp_priority: str = "stop_first",
    use_current_meta: bool = DEFAULT_USE_CURRENT_META,
    buy_friction_pct: float = DEFAULT_BUY_FRICTION_PCT,
    sell_friction_pct: float = DEFAULT_SELL_FRICTION_PCT,
) -> tuple[pd.DataFrame, dict]:
    if end_dt <= start_dt:
        raise ValueError("end 必须晚于 start")
    if hold_days < 1:
        raise ValueError("hold_days 必须 >= 1")
    if exit_mode not in {"close_only", "sltp"}:
        raise ValueError("exit_mode 必须是 close_only 或 sltp")
    if sltp_priority not in {"stop_first", "take_first"}:
        raise ValueError("sltp_priority 必须是 stop_first 或 take_first")
    if stop_loss_pct > 0:
        raise ValueError("stop_loss_pct 必须 <= 0，0 表示不设止损")
    if take_profit_pct < 0:
        raise ValueError("take_profit_pct 必须 >= 0，0 表示不设止盈")
    if buy_friction_pct < 0 or sell_friction_pct < 0:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 >= 0")
    if buy_friction_pct >= 100 or sell_friction_pct >= 100:
        raise ValueError("buy_friction_pct / sell_friction_pct 必须 < 100")

    symbols, name_map = _build_universe(board=board, sample_size=sample_size)
    if not symbols:
        raise RuntimeError("股票池为空")
    print(f"[backtest] 股票池={len(symbols)} (board={board}, sample_size={sample_size})")

    prefetch_start = start_dt - timedelta(days=trading_days * 3)
    prefetch_end = end_dt + timedelta(days=hold_days * 3 + 30)

    all_df_map: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    bench_df: pd.DataFrame | None = None
    snapshot_rows_total = 0
    snapshot_used = False

    if snapshot_dir is not None:
        snapshot_dir = Path(snapshot_dir).resolve()
        print(f"[backtest] 使用本地快照: {snapshot_dir}")
        all_df_map, snapshot_rows_total = _load_snapshot_hist_map(
            snapshot_dir, symbols_filter=set(symbols)
        )
        bench_df = _load_snapshot_benchmark(snapshot_dir)
        snapshot_used = True
        if not all_df_map:
            raise RuntimeError(f"快照无可用历史数据: {snapshot_dir}")
        print(
            f"[backtest] 快照载入完成: ok={len(all_df_map)}, rows={snapshot_rows_total}"
        )
    else:
        print(f"[backtest] 开始拉取历史日线: symbols={len(symbols)}, workers={max_workers}")
        with ThreadPoolExecutor(max_workers=max(int(max_workers), 1)) as ex:
            futures = {
                ex.submit(_fetch_hist_norm, sym, prefetch_start, prefetch_end): sym for sym in symbols
            }
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
        print(f"[backtest] 历史拉取完成: ok={len(all_df_map)}, fail={len(failures)}")

    if bench_df is None or bench_df.empty:
        try:
            bench_raw = fetch_index_hist("000001", prefetch_start, prefetch_end)
        except Exception as exc:
            raise RuntimeError(
                "回测需要大盘交易日历与基准收益，请先配置可用的 TUSHARE_TOKEN。"
            ) from exc
        bench_df = normalize_hist_from_fetch(bench_raw).sort_values("date").copy()
        bench_df["date"] = pd.to_datetime(bench_df["date"], errors="coerce").dt.date
        bench_df = bench_df.dropna(subset=["date"]).reset_index(drop=True)

    trade_dates = [d for d in bench_df["date"].tolist() if start_dt <= d <= end_dt]
    print(f"[backtest] DEBUG: start={start_dt}, end={end_dt}, bench_min={bench_df['date'].min()}, bench_max={bench_df['date'].max()}")
    print(f"[backtest] DEBUG: trade_dates count={len(trade_dates)}")
    if len(trade_dates) <= hold_days + 1:
        raise RuntimeError(f"回测区间交易日过少({len(trade_dates)})，无法计算 forward return (hold_days={hold_days}，需至少 {hold_days + 2} 个交易日)")

    if use_current_meta:
        market_cap_map = fetch_market_cap_map()
        sector_map = fetch_sector_map()
        print(
            "[backtest] ⚠️ 使用当前截面市值/行业映射（会引入 look-ahead bias）"
        )
        if not market_cap_map:
            print("[backtest] ⚠️ 当前市值映射为空，Layer1 市值过滤将被跳过")
    else:
        market_cap_map = {}
        sector_map = {}
        print(
            "[backtest] 偏差抑制口径：关闭当前截面市值/行业映射过滤 "
            "(L1 市值过滤 + L3 行业共振过滤)"
        )
    base_cfg = FunnelConfig(trading_days=trading_days)
    _apply_funnel_cfg_overrides(base_cfg)

    records: list[TradeRecord] = []
    signal_days = 0
    eval_days = 0
    ohlc_lookup_cache: dict[str, dict[date, tuple[float, float, float, float]]] = {}

    max_idx = len(trade_dates) - hold_days - 1  # -1: 信号次日才能入场，需多预留一天
    for idx in range(max_idx):
        signal_date = trade_dates[idx]
        entry_target_date = trade_dates[idx + 1]      # 信号日收盘后才能看到信号，次日开盘才能买入
        exit_anchor_date = trade_dates[idx + 1 + hold_days]  # 从实际入场日起计算持有天数

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
        selected_for_ai, p_score_map, track_map = _select_ai_input_codes(
            result=result,
            day_df_map=day_df_map,
            sector_map=sector_map,
            regime=regime,
            selection_mode=FUNNEL_AI_SELECTION_MODE,
        )
        if not selected_for_ai:
            continue

        ranked_codes = selected_for_ai if int(top_n) <= 0 else selected_for_ai[:top_n]

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
            actual_exit_idx = actual_entry_idx + hold_days
            if actual_exit_idx >= len(trade_dates):
                continue  # 剩余交易日不足以覆盖完整持有期
            actual_exit_anchor = trade_dates[actual_exit_idx]

            if exit_mode == "close_only":
                # 兼容旧口径：持有 N 个市场交易日后按 anchor 日（或其后首个可得日）收盘离场。
                exit_close, exit_date = _close_on_or_after(full_df, actual_exit_anchor)
            else:
                # sltp 口径：仅在实际入场日到退出锚点日的市场交易日窗口内检查触发。
                exit_close = None
                exit_date = None
                market_window = trade_dates[actual_entry_idx : actual_exit_idx + 1]
                day_ohlc = ohlc_lookup_cache.get(code)
                if day_ohlc is None:
                    day_ohlc = _build_daily_ohlc_lookup(full_df)
                    ohlc_lookup_cache[code] = day_ohlc

                sl_price = (
                    entry_close * (1.0 + stop_loss_pct / 100.0)
                    if stop_loss_pct < 0
                    else None
                )
                tp_price = (
                    entry_close * (1.0 + take_profit_pct / 100.0)
                    if take_profit_pct > 0
                    else None
                )

                for mkt_day in market_window:
                    candle = day_ohlc.get(mkt_day)
                    if candle is None:
                        continue
                    open_px, high, low, _ = candle

                    if sltp_priority == "stop_first":
                        checks = [("sl", sl_price), ("tp", tp_price)]
                    else:
                        checks = [("tp", tp_price), ("sl", sl_price)]

                    hit = False
                    for kind, px in checks:
                        if px is None:
                            continue
                        if kind == "sl" and low <= px:
                            # 若开盘已跳空跌破止损，只能按开盘附近成交；否则按止损价成交。
                            exit_close = px if open_px >= px else open_px
                            exit_date = mkt_day
                            hit = True
                            break
                        if kind == "tp" and high >= px:
                            # 若开盘已跳空高开越过止盈，按开盘附近成交；否则按止盈价成交。
                            exit_close = px if open_px <= px else open_px
                            exit_date = mkt_day
                            hit = True
                            break
                    if hit:
                        break

                if exit_close is None:
                    # 未触发则按窗口最后一天(含)及之前最近可得收盘离场，不延长持仓天数。
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
        "sltp_priority": sltp_priority,
        "use_current_meta": bool(use_current_meta),
        "buy_friction_pct": float(buy_friction_pct),
        "sell_friction_pct": float(sell_friction_pct),
    }
    if not trades_df.empty:
        ret = pd.to_numeric(trades_df["ret_pct"], errors="coerce").dropna()
        var95_ret_pct, cvar95_ret_pct = _calc_cvar95_pct(ret)
        summary.update(
            {
                "win_rate_pct": float((ret > 0).mean() * 100.0),
                "avg_ret_pct": float(ret.mean()),
                "median_ret_pct": float(ret.median()),
                "q25_ret_pct": float(ret.quantile(0.25)),
                "q75_ret_pct": float(ret.quantile(0.75)),
                "max_drawdown_pct": _calc_max_drawdown_pct(ret),
                "var95_ret_pct": var95_ret_pct,
                "cvar95_ret_pct": cvar95_ret_pct,
                "max_consecutive_losses": _calc_max_consecutive_losses(ret),
                "sharpe_ratio": _calc_sharpe_ratio(ret, hold_days=hold_days),
                "calmar_ratio": _calc_calmar_ratio(ret, hold_days=hold_days),
                "stratified": _calc_stratified_stats(trades_df),
            }
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
                "stratified": {},
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
    ann_std = std_pct * (periods_per_year ** 0.5) / 100.0
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
    ann_te = excess_std * (periods_per_year ** 0.5) / 100.0
    return float(ann_excess / ann_te)


def _calc_stratified_stats(trades_df: pd.DataFrame) -> dict[str, dict]:
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
            "sharpe_ratio": _calc_sharpe_ratio(ret),
            "calmar_ratio": _calc_calmar_ratio(ret),
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


def _build_summary_md(summary: dict) -> str:
    use_current_meta = bool(summary.get("use_current_meta"))
    meta_mode = (
        "current_snapshot (⚠️ look-ahead bias)"
        if use_current_meta
        else "disabled_current_snapshot_filters (bias-reduced)"
    )
    notes = [
        "- 该回测仅使用日线数据（qfq），不含盘口逐笔成交与涨跌停成交约束。",
        "- 入场口径：信号日收盘后出信号，次日开盘价买入（消除前视偏差）。",
        "- 已纳入双边交易摩擦成本（买入/卖出各0.5%），用于近似滑点 + 佣金 + 税费影响。",
        "- ⚠️ 仍存在幸存者偏差：股票池来自当前在市样本，未包含历史退市股票。",
    ]
    if use_current_meta:
        notes.append(
            "- ⚠️ 市值/行业映射采用当前截面，会引入 look-ahead bias "
            "（市值穿越与行业漂移）；该结果仅用于参数方向验证。"
        )
    else:
        notes.append(
            "- 本次已关闭当前截面市值/行业映射过滤（Layer1 市值 + Layer3 行业共振），"
            "用于降低前视偏差。"
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
            f"- 止损线: {_fmt_metric(summary.get('stop_loss_pct'), 1)}%",
            f"- 止盈线: {_fmt_metric(summary.get('take_profit_pct'), 1)}%",
            f"- 日内触发优先级: {summary.get('sltp_priority')}",
            f"- 买入摩擦成本: {_fmt_metric(summary.get('buy_friction_pct'), 3)}%",
            f"- 卖出摩擦成本: {_fmt_metric(summary.get('sell_friction_pct'), 3)}%",
            f"- 元数据口径: {meta_mode}",
            f"- 成交样本: {summary.get('trades')}",
            "",
            "## 收益统计",
            f"- 胜率: {_fmt_metric(summary.get('win_rate_pct'), 2)}%",
            f"- 平均收益: {_fmt_metric(summary.get('avg_ret_pct'), 3)}%",
            f"- 中位收益: {_fmt_metric(summary.get('median_ret_pct'), 3)}%",
            f"- 25%分位: {_fmt_metric(summary.get('q25_ret_pct'), 3)}%",
            f"- 75%分位: {_fmt_metric(summary.get('q75_ret_pct'), 3)}%",
            "",
            "## 风险调整指标",
            f"- 夏普比 (Sharpe Ratio): {_fmt_metric(summary.get('sharpe_ratio'), 3)}",
            f"- 卡玛比 (Calmar Ratio): {_fmt_metric(summary.get('calmar_ratio'), 3)}",
            f"- 最大回撤(逐笔复利): {_fmt_metric(summary.get('max_drawdown_pct'), 3)}%",
            f"- VaR95(单笔收益): {_fmt_metric(summary.get('var95_ret_pct'), 3)}%",
            f"- CVaR95(最差5%%均值): {_fmt_metric(summary.get('cvar95_ret_pct'), 3)}%",
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

    lines.extend(["", "## 说明", *notes])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wyckoff Funnel 日线轻量回测器")
    parser.add_argument("--start", required=True, help="起始日期: YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期: YYYY-MM-DD 或 YYYYMMDD")
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
    parser.add_argument("--board", choices=["all", "main", "chinext"], default="all")
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
        choices=["close_only", "sltp"],
        default=DEFAULT_EXIT_MODE,
        help=f"离场模式：close_only=仅按持有天数收盘离场；sltp=启用日内止盈止损 (default: {DEFAULT_EXIT_MODE})",
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
        "--sltp-priority",
        choices=["stop_first", "take_first"],
        default="stop_first",
        help="同一交易日同时触及止损/止盈时的判定顺序",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="",
        help="本地快照目录（由 wyckoff_funnel 的 FUNNEL_EXPORT_FULL_FETCH 生成）",
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
    args = parser.parse_args()

    start_dt = _parse_date(args.start)
    end_dt = _parse_date(args.end)
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    hold_days_list = (
        _parse_hold_days_list(args.hold_days_list)
        if str(args.hold_days_list).strip()
        else [int(args.hold_days)]
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
                sltp_priority=args.sltp_priority,
                use_current_meta=args.use_current_meta,
                buy_friction_pct=args.buy_friction_pct,
                sell_friction_pct=args.sell_friction_pct,
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
        raise RuntimeError(
            "多周期回测全部失败，请检查日期区间、快照覆盖范围或 TUSHARE_TOKEN。"
        ) from last_error

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
