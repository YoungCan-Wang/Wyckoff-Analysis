# -*- coding: utf-8 -*-
"""
Wyckoff Funnel 4 层漏斗筛选引擎

Layer 1: 剥离垃圾 (ST / 北交所 / 科创板 / 市值 / 成交额)
Layer 2: 强弱甄别 (MA50>MA200 多头排列, 或大盘连跌时守住 MA20)
Layer 3: 板块共振 (行业分布 Top-N)
Layer 4: 威科夫狙击 (Spring / LPS / Effort vs Result)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
import pandas as pd


def normalize_hist_from_fetch(df: pd.DataFrame) -> pd.DataFrame:
    """将 fetch_a_share_csv._fetch_hist 返回的 DataFrame 转为筛选器所需格式。"""
    col_map = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_chg",
    }
    out = df.rename(columns=col_map)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"] if c in out.columns]
    out = out[keep].copy()
    if "pct_chg" not in out.columns and "close" in out.columns:
        out["pct_chg"] = out["close"].astype(float).pct_change() * 100
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class FunnelConfig:
    trading_days: int = 500

    # Layer 1
    min_market_cap_yi: float = 20.0
    min_avg_amount_wan: float = 5000.0
    amount_avg_window: int = 20

    # Layer 2
    ma_short: int = 50
    ma_long: int = 200
    ma_hold: int = 20
    bench_drop_days: int = 3
    bench_drop_threshold: float = -2.0

    # Layer 3
    top_n_sectors: int = 3

    # Layer 4 - Spring
    spring_support_window: int = 60
    spring_vol_ratio: float = 1.0

    # Layer 4 - LPS
    lps_lookback: int = 3
    lps_ma: int = 20
    lps_ma_tolerance: float = 0.01
    lps_vol_dry_ratio: float = 0.35
    lps_vol_ref_window: int = 60

    # Layer 4 - Effort vs Result
    evr_lookback: int = 3
    evr_vol_ratio: float = 2.0
    evr_vol_window: int = 20
    evr_max_drop: float = 2.0


class FunnelResult(NamedTuple):
    layer1_symbols: list[str]
    layer2_symbols: list[str]
    layer3_symbols: list[str]
    top_sectors: list[str]
    triggers: dict[str, list[tuple[str, float]]]


# ---------------------------------------------------------------------------
# Layer 1: 剥离垃圾
# ---------------------------------------------------------------------------

def _is_main_or_chinext(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301"))


def layer1_filter(
    symbols: list[str],
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
) -> list[str]:
    """
    硬过滤：剔除 ST、北交所/科创板、市值<阈值、近期均成交额<阈值。
    market_cap_map 单位：亿元。若 market_cap_map 为空则跳过市值过滤。
    """
    cap_available = bool(market_cap_map)
    passed: list[str] = []
    for sym in symbols:
        if not _is_main_or_chinext(sym):
            continue
        name = name_map.get(sym, "")
        if "ST" in name.upper():
            continue
        if cap_available:
            cap = market_cap_map.get(sym, 0.0)
            if cap < cfg.min_market_cap_yi:
                continue
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        df_sorted = df.sort_values("date")
        if "amount" in df_sorted.columns:
            avg_amt = df_sorted["amount"].tail(cfg.amount_avg_window).mean()
            if pd.notna(avg_amt) and avg_amt < cfg.min_avg_amount_wan * 10000:
                continue
        passed.append(sym)
    return passed


# ---------------------------------------------------------------------------
# Layer 2: 强弱甄别
# ---------------------------------------------------------------------------

def layer2_strength(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
) -> list[str]:
    """
    MA50 > MA200 多头排列，OR 大盘连跌时仍守住 MA20。
    """
    bench_dropping = False
    if bench_df is not None and not bench_df.empty:
        bench_sorted = bench_df.sort_values("date")
        if len(bench_sorted) >= cfg.bench_drop_days:
            recent_bench = bench_sorted.tail(cfg.bench_drop_days)
            bench_cum = (recent_bench["pct_chg"].dropna() / 100.0 + 1).prod() - 1
            bench_dropping = bench_cum * 100 <= cfg.bench_drop_threshold

    passed: list[str] = []
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or len(df) < cfg.ma_long:
            continue
        df_sorted = df.sort_values("date").copy()
        close = df_sorted["close"].astype(float)
        ma_short = close.rolling(cfg.ma_short).mean()
        ma_long = close.rolling(cfg.ma_long).mean()
        last_ma_short = ma_short.iloc[-1]
        last_ma_long = ma_long.iloc[-1]

        bullish_alignment = pd.notna(last_ma_short) and pd.notna(last_ma_long) and last_ma_short > last_ma_long

        holding_ma20 = False
        if bench_dropping:
            ma_hold = close.rolling(cfg.ma_hold).mean()
            last_ma_hold = ma_hold.iloc[-1]
            last_close = close.iloc[-1]
            if pd.notna(last_ma_hold) and last_close >= last_ma_hold:
                holding_ma20 = True

        if bullish_alignment or holding_ma20:
            passed.append(sym)
    return passed


# ---------------------------------------------------------------------------
# Layer 3: 板块共振
# ---------------------------------------------------------------------------

def layer3_sector_resonance(
    symbols: list[str],
    sector_map: dict[str, str],
    cfg: FunnelConfig,
) -> tuple[list[str], list[str]]:
    """
    统计行业分布，保留 Top-N 行业内的股票。
    返回 (过滤后 symbols, top_sectors)。
    """
    counts: dict[str, int] = {}
    for sym in symbols:
        sector = sector_map.get(sym, "")
        if sector:
            counts[sector] = counts.get(sector, 0) + 1

    if not counts:
        return symbols, []

    top_sectors = [s for s, _ in sorted(counts.items(), key=lambda x: -x[1])[:cfg.top_n_sectors]]
    top_set = set(top_sectors)
    filtered = [sym for sym in symbols if sector_map.get(sym, "") in top_set]
    return filtered, top_sectors


# ---------------------------------------------------------------------------
# Layer 4: 威科夫狙击
# ---------------------------------------------------------------------------

def _detect_spring(df: pd.DataFrame, cfg: FunnelConfig) -> float | None:
    """
    Spring（终极震仓）：前一日 low 跌破近 N 日支撑位，今日收盘收回，且放量。
    返回 score（收回幅度%）或 None。
    """
    if len(df) < cfg.spring_support_window + 2:
        return None
    df_s = df.sort_values("date")
    support_zone = df_s.iloc[-(cfg.spring_support_window + 1):-1]
    support_level = support_zone["close"].min()
    prev = df_s.iloc[-2]
    last = df_s.iloc[-1]

    if prev["low"] >= support_level:
        return None
    if last["close"] <= support_level:
        return None
    vol_avg = df_s["volume"].tail(5).iloc[:-1].mean()
    if vol_avg <= 0 or last["volume"] < vol_avg * cfg.spring_vol_ratio:
        return None
    recovery = (last["close"] - support_level) / support_level * 100
    return float(recovery)


def _detect_lps(df: pd.DataFrame, cfg: FunnelConfig) -> float | None:
    """
    LPS（最后支撑点缩量）：近 N 日回踩 MA20 且缩量。
    返回 score（缩量比）或 None。
    """
    if len(df) < max(cfg.lps_vol_ref_window, cfg.lps_ma) + cfg.lps_lookback:
        return None
    df_s = df.sort_values("date").copy()
    close = df_s["close"].astype(float)
    ma = close.rolling(cfg.lps_ma).mean()
    last_ma = ma.iloc[-1]
    if pd.isna(last_ma) or last_ma <= 0:
        return None

    recent = df_s.tail(cfg.lps_lookback)
    last_close = close.iloc[-1]
    if last_close < last_ma:
        return None

    low_near_ma = recent["low"].min()
    if abs(low_near_ma - last_ma) / last_ma > cfg.lps_ma_tolerance:
        return None

    recent_max_vol = recent["volume"].max()
    ref_max_vol = df_s["volume"].tail(cfg.lps_vol_ref_window).max()
    if ref_max_vol <= 0:
        return None
    vol_ratio = recent_max_vol / ref_max_vol
    if vol_ratio > cfg.lps_vol_dry_ratio:
        return None
    return float(vol_ratio)


def _detect_evr(df: pd.DataFrame, cfg: FunnelConfig) -> float | None:
    """
    Effort vs Result（努力无结果）：底部放巨量但价格不跌。
    返回 score（量比）或 None。
    """
    if len(df) < cfg.evr_vol_window + cfg.evr_lookback:
        return None
    df_s = df.sort_values("date")
    recent = df_s.tail(cfg.evr_lookback)

    vol_avg_recent = recent["volume"].mean()
    vol_avg_ref = df_s["volume"].tail(cfg.evr_vol_window).iloc[:cfg.evr_vol_window - cfg.evr_lookback].mean()
    if vol_avg_ref <= 0:
        return None
    vol_ratio = vol_avg_recent / vol_avg_ref
    if vol_ratio < cfg.evr_vol_ratio:
        return None

    max_drop = recent["pct_chg"].min()
    if pd.isna(max_drop) or max_drop < -cfg.evr_max_drop:
        return None

    close_start = df_s.iloc[-(cfg.evr_lookback + 1)]["close"]
    close_end = df_s.iloc[-1]["close"]
    if close_end < close_start:
        return None

    return float(vol_ratio)


def layer4_triggers(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
) -> dict[str, list[tuple[str, float]]]:
    """
    在最终候选集上运行 Spring / LPS / EffortVsResult 检测。
    """
    results: dict[str, list[tuple[str, float]]] = {
        "spring": [],
        "lps": [],
        "evr": [],
    }
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        score = _detect_spring(df, cfg)
        if score is not None:
            results["spring"].append((sym, score))
        score = _detect_lps(df, cfg)
        if score is not None:
            results["lps"].append((sym, score))
        score = _detect_evr(df, cfg)
        if score is not None:
            results["evr"].append((sym, score))
    return results


# ---------------------------------------------------------------------------
# run_funnel: 串联 4 层
# ---------------------------------------------------------------------------

def run_funnel(
    all_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    cfg: FunnelConfig | None = None,
) -> FunnelResult:
    if cfg is None:
        cfg = FunnelConfig()

    l1 = layer1_filter(all_symbols, name_map, market_cap_map, df_map, cfg)
    l2 = layer2_strength(l1, df_map, bench_df, cfg)
    l3, top_sectors = layer3_sector_resonance(l2, sector_map, cfg)
    triggers = layer4_triggers(l3, df_map, cfg)

    return FunnelResult(
        layer1_symbols=l1,
        layer2_symbols=l2,
        layer3_symbols=l3,
        top_sectors=top_sectors,
        triggers=triggers,
    )
