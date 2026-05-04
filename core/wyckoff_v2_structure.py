"""Structure-aware Wyckoff funnel variant.

This module is intentionally independent from the production V1 funnel.  It
reuses the current L1/L2/L3 filters, then swaps L4 for a dynamic trading-range
detector so the new strategy can run as a shadow strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import pandas as pd

from core.wyckoff_engine import (
    FunnelConfig,
    FunnelResult,
    _sorted_if_needed,
    detect_accum_stage,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer5_exit_signals,
)

STRATEGY_ID = "wyckoff_v2_structure"


@dataclass(frozen=True)
class TradingRange:
    support: float
    resistance: float
    mid: float
    width_pct: float
    support_tests: int
    resistance_tests: int
    quality_score: float


class StructureTriggerResult(NamedTuple):
    triggers: dict[str, list[tuple[str, float]]]
    trading_ranges: dict[str, TradingRange]
    stage_map: dict[str, str]


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _ensure_pct_chg(df: pd.DataFrame) -> pd.Series:
    if "pct_chg" in df.columns:
        pct = _to_numeric(df["pct_chg"])
        if not pct.isna().all():
            return pct
    return _to_numeric(df["close"]).pct_change() * 100.0


def _last_ref_volume_ratio(volume: pd.Series, window: int) -> float | None:
    if len(volume) < window + 1:
        return None
    ref = _to_numeric(volume).tail(window + 1).iloc[:-1].dropna()
    if ref.empty:
        return None
    ref_mean = float(ref.mean())
    if ref_mean <= 0:
        return None
    last = volume.iloc[-1]
    if pd.isna(last):
        return None
    return float(last) / ref_mean


def _recent_ref_volume_ratio(volume: pd.Series, recent_n: int, ref_n: int) -> float | None:
    recent_n = max(int(recent_n), 1)
    ref_n = max(int(ref_n), recent_n + 1)
    if len(volume) < ref_n + recent_n:
        return None
    recent = _to_numeric(volume).tail(recent_n).dropna()
    ref = _to_numeric(volume).tail(ref_n + recent_n).iloc[:-recent_n].dropna()
    if recent.empty or ref.empty:
        return None
    ref_max = float(ref.max())
    if ref_max <= 0:
        return None
    return float(recent.max()) / ref_max


def _swing_values(series: pd.Series, *, kind: str, window: int) -> list[float]:
    values = _to_numeric(series).reset_index(drop=True)
    out: list[float] = []
    w = max(int(window), 1)
    if len(values) < w * 2 + 1:
        return out
    for i in range(w, len(values) - w):
        current = values.iloc[i]
        if pd.isna(current):
            continue
        span = values.iloc[i - w : i + w + 1].dropna()
        if span.empty:
            continue
        if kind == "low" and float(current) <= float(span.min()):
            out.append(float(current))
        elif kind == "high" and float(current) >= float(span.max()):
            out.append(float(current))
    return out


def identify_trading_range(
    df: pd.DataFrame,
    cfg: FunnelConfig | None = None,
    *,
    lookback: int = 90,
    swing_window: int = 3,
    exclude_last: int = 1,
) -> TradingRange | None:
    """Identify a recent Wyckoff trading range from swing highs/lows.

    `exclude_last=1` is the default because trigger detection should compare
    today's bar against the range that was visible before today's close.
    """

    if df is None or df.empty:
        return None
    cfg = cfg or FunnelConfig()
    min_bars = max(40, swing_window * 2 + 20)
    if len(df) < min_bars + max(int(exclude_last), 0):
        return None

    df_s = _sorted_if_needed(df).copy()
    if exclude_last > 0:
        df_s = df_s.iloc[:-exclude_last]
    zone = df_s.tail(max(int(lookback), min_bars)).copy()
    if len(zone) < min_bars:
        return None

    high = _to_numeric(zone["high"])
    low = _to_numeric(zone["low"])
    close = _to_numeric(zone["close"])
    if high.isna().all() or low.isna().all() or close.isna().all():
        return None

    swing_lows = _swing_values(low, kind="low", window=swing_window)
    swing_highs = _swing_values(high, kind="high", window=swing_window)
    support = float(pd.Series(swing_lows[-5:]).median()) if len(swing_lows) >= 2 else float(low.quantile(0.10))
    resistance = float(pd.Series(swing_highs[-5:]).median()) if len(swing_highs) >= 2 else float(high.quantile(0.90))
    if support <= 0 or resistance <= support:
        return None

    width_pct = (resistance - support) / support * 100.0
    max_width = min(max(float(getattr(cfg, "spring_tr_max_range_pct", 30.0)) * 1.5, 24.0), 55.0)
    if width_pct < 4.0 or width_pct > max_width:
        return None

    first_close = close.dropna().iloc[0]
    last_close = close.dropna().iloc[-1]
    if first_close <= 0:
        return None
    drift_pct = abs((float(last_close) - float(first_close)) / float(first_close) * 100.0)
    max_drift = max(float(getattr(cfg, "spring_tr_max_drift_pct", 12.0)) * 1.5, 18.0)
    if drift_pct > max_drift:
        return None

    tolerance = 0.035
    support_tests = int((low <= support * (1.0 + tolerance)).sum())
    resistance_tests = int((high >= resistance * (1.0 - tolerance)).sum())
    if support_tests < 2 or resistance_tests < 2:
        return None

    width_score = max(0.0, 1.0 - abs(width_pct - 18.0) / 30.0)
    test_score = min((support_tests + resistance_tests) / 8.0, 1.0)
    drift_score = max(0.0, 1.0 - drift_pct / max_drift)
    quality_score = 0.45 * test_score + 0.35 * width_score + 0.20 * drift_score
    mid = support + (resistance - support) / 2.0
    return TradingRange(
        support=support,
        resistance=resistance,
        mid=mid,
        width_pct=width_pct,
        support_tests=support_tests,
        resistance_tests=resistance_tests,
        quality_score=float(quality_score),
    )


def _infer_stage(df: pd.DataFrame, tr: TradingRange, trigger_keys: set[str]) -> str:
    close = _to_numeric(df["close"])
    last_close = float(close.iloc[-1])
    width = tr.resistance - tr.support
    if last_close >= tr.resistance:
        return "Markup"
    if "spring" in trigger_keys:
        return "Accum_C"
    if "lps" in trigger_keys:
        return "Accum_C"
    if last_close <= tr.support + width * 0.35:
        return "Accum_B"
    return "Accum_A"


def detect_structure_triggers(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    *,
    lookback: int = 90,
) -> StructureTriggerResult:
    """Run dynamic-TR Spring / SOS / LPS / EVR detection."""

    triggers: dict[str, list[tuple[str, float]]] = {"sos": [], "spring": [], "lps": [], "evr": []}
    ranges: dict[str, TradingRange] = {}
    stage_map: dict[str, str] = {}

    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty or len(df) < 60:
            continue
        df_s = _sorted_if_needed(df).copy()
        tr = identify_trading_range(df_s, cfg, lookback=lookback, exclude_last=1)
        if tr is None:
            continue

        high = _to_numeric(df_s["high"])
        low = _to_numeric(df_s["low"])
        close = _to_numeric(df_s["close"])
        volume = _to_numeric(df_s["volume"])
        pct_chg = _ensure_pct_chg(df_s)
        if close.isna().all() or low.isna().all() or high.isna().all() or volume.isna().all():
            continue

        ranges[sym] = tr
        width = tr.resistance - tr.support
        last_close = float(close.iloc[-1])
        last_low = float(low.iloc[-1])
        prev_low = float(low.iloc[-2]) if len(low) >= 2 else last_low
        last_pct = float(pct_chg.iloc[-1]) if pd.notna(pct_chg.iloc[-1]) else 0.0
        hit_keys: set[str] = set()

        vol_ratio_sos = _last_ref_volume_ratio(volume, max(int(cfg.sos_vol_window), 5))
        if vol_ratio_sos is not None:
            breakout_tolerance = float(getattr(cfg, "sos_breakout_tolerance", 0.01))
            structure_breakout = last_close >= tr.resistance * (1.0 - breakout_tolerance)
            enough_push = last_pct >= float(getattr(cfg, "sos_pct_min", 6.0))
            enough_volume = vol_ratio_sos >= float(getattr(cfg, "sos_vol_ratio", 2.0))
            if structure_breakout and enough_push and enough_volume:
                score = vol_ratio_sos + max((last_close - tr.resistance) / tr.resistance * 100.0, 0.0)
                score += tr.quality_score
                triggers["sos"].append((sym, float(score)))
                hit_keys.add("sos")

        vol_ratio_spring = _last_ref_volume_ratio(volume, 5)
        pierced = min(prev_low, last_low) <= tr.support * 0.995
        recovered = last_close > tr.support * 1.005
        still_in_range = last_close < tr.mid + width * 0.25
        if (
            vol_ratio_spring is not None
            and pierced
            and recovered
            and still_in_range
            and vol_ratio_spring >= float(getattr(cfg, "spring_vol_ratio", 1.1))
        ):
            recovery = (last_close - tr.support) / tr.support * 100.0
            score = recovery + tr.quality_score
            triggers["spring"].append((sym, float(score)))
            hit_keys.add("spring")

        dry_ratio = _recent_ref_volume_ratio(
            volume,
            max(int(getattr(cfg, "lps_lookback", 3)), 1),
            max(int(getattr(cfg, "lps_vol_ref_window", 60)), 10),
        )
        recent_lows = low.tail(max(int(getattr(cfg, "lps_lookback", 3)), 1))
        near_support = float(recent_lows.min()) <= tr.support + width * 0.35
        holds_support = last_close > tr.support
        if dry_ratio is not None and near_support and holds_support and dry_ratio <= float(cfg.lps_vol_dry_ratio):
            score = (1.0 - dry_ratio) + tr.quality_score
            triggers["lps"].append((sym, float(score)))
            hit_keys.add("lps")

        vol_ratio_evr = _last_ref_volume_ratio(volume, max(int(cfg.evr_vol_window), 10))
        in_lower_range = last_close <= tr.mid
        if (
            getattr(cfg, "enable_evr_trigger", False)
            and vol_ratio_evr is not None
            and vol_ratio_evr >= float(cfg.evr_vol_ratio)
            and in_lower_range
            and -float(cfg.evr_max_drop) <= last_pct <= float(cfg.evr_max_rise)
            and last_close >= tr.support * 0.98
        ):
            score = vol_ratio_evr + tr.quality_score
            triggers["evr"].append((sym, float(score)))
            hit_keys.add("evr")

        stage_map[sym] = _infer_stage(df_s, tr, hit_keys)

    return StructureTriggerResult(triggers=triggers, trading_ranges=ranges, stage_map=stage_map)


def run_structure_funnel(
    all_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    cfg: FunnelConfig | None = None,
    *,
    financial_map: dict[str, dict] | None = None,
) -> FunnelResult:
    """Run the shadow V2 funnel.

    V1 remains the benchmark.  V2 deliberately reuses V1 L1/L2/L3 so the only
    strategy difference is structural L4 trigger detection.
    """

    cfg = cfg or FunnelConfig()
    prepared_df_map: dict[str, pd.DataFrame] = {
        sym: _sorted_if_needed(df) for sym, df in df_map.items() if df is not None and not df.empty
    }
    l1 = layer1_filter(
        all_symbols,
        name_map,
        market_cap_map,
        prepared_df_map,
        cfg,
        financial_map=financial_map,
    )
    l2, channel_map = layer2_strength_detailed(
        l1,
        prepared_df_map,
        bench_df,
        cfg,
        rps_universe=list(prepared_df_map.keys()),
    )
    l3, top_sectors = layer3_sector_resonance(
        l2,
        sector_map,
        cfg,
        base_symbols=l1,
        df_map=prepared_df_map,
    )
    structure = detect_structure_triggers(l3, prepared_df_map, cfg)

    accum_stage_map = detect_accum_stage(l2, prepared_df_map, cfg)
    stage_map = {**accum_stage_map, **structure.stage_map}
    markup_symbols = [code for code, stage in stage_map.items() if stage == "Markup"]
    channel_map_v2 = dict(channel_map)
    for code in structure.trading_ranges:
        existing = str(channel_map_v2.get(code, "")).strip()
        channel_map_v2[code] = f"{existing}+结构TR" if existing else "结构TR"

    exit_signals = layer5_exit_signals(l2 + markup_symbols, prepared_df_map, accum_stage_map, cfg)
    return FunnelResult(
        layer1_symbols=l1,
        layer2_symbols=l2,
        layer3_symbols=l3,
        top_sectors=top_sectors,
        triggers=structure.triggers,
        stage_map=stage_map,
        markup_symbols=markup_symbols,
        exit_signals=exit_signals,
        channel_map=channel_map_v2,
    )


__all__ = [
    "STRATEGY_ID",
    "StructureTriggerResult",
    "TradingRange",
    "detect_structure_triggers",
    "identify_trading_range",
    "run_structure_funnel",
]
