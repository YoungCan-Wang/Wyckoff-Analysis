"""
板块轮动水温计

用途：
1. 基于全市场个股日线，识别行业处于：
   - 连续一致高潮
   - 主线健康推进
   - 主流分歧回撤
   - 退潮派发风险
   - 中性混沌
2. 为 Funnel 与 Step3 提供“行业状态 + 证据 + 动作指引”。
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

SECTOR_STATE_LABELS: dict[str, str] = {
    "CONSENSUS_CLIMAX": "连续一致高潮",
    "HEALTHY_MAINLINE": "主线健康推进",
    "DISAGREEMENT_PULLBACK": "主流分歧回撤",
    "DISTRIBUTION_RISK": "退潮派发风险",
    "NEUTRAL_MIXED": "中性混沌",
}

SECTOR_STATE_GUIDANCE: dict[str, str] = {
    "CONSENSUS_CLIMAX": "板块处于连续一致高潮，默认禁止 Attack，只允许等待分歧或用 Probe 低吸内切股。",
    "HEALTHY_MAINLINE": "板块主线健康推进，可正常评估 Probe / Attack，但仍需服从个股量价结构。",
    "DISAGREEMENT_PULLBACK": "主流板块分歧回撤，优先寻找 Spring / LPS / Test，允许用 Probe 试探水温。",
    "DISTRIBUTION_RISK": "板块出现退潮派发风险，除非个股结构极强，否则优先进非操作区。",
    "NEUTRAL_MIXED": "板块状态混沌，行业层只作次级参考，个股结构优先。",
}

SECTOR_STATE_SCORE_BONUS: dict[str, float] = {
    # 2025-10 ~ 2026-04 实测校准（analyze_sector_reversal.py）:
    #   DISAGREEMENT_PULLBACK 后续3日均收益-0.51%、胜率仅50% → 大幅降低
    #   CONSENSUS_CLIMAX 后续3日跌>2%概率29.8% → 加大惩罚
    "DISAGREEMENT_PULLBACK": 0.01,
    "HEALTHY_MAINLINE": 0.03,
    "CONSENSUS_CLIMAX": -0.08,
    "DISTRIBUTION_RISK": -0.10,
    "NEUTRAL_MIXED": 0.0,
}

_OVERVIEW_STATE_ORDER = [
    "DISAGREEMENT_PULLBACK",
    "HEALTHY_MAINLINE",
    "CONSENSUS_CLIMAX",
    "DISTRIBUTION_RISK",
]


def _safe_return(series: pd.Series, lookback: int) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) <= lookback:
        return None
    start = float(s.iloc[-lookback - 1])
    end = float(s.iloc[-1])
    if start == 0:
        return None
    return (end - start) / start * 100.0


def _safe_median(values: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None and pd.notna(v)]
    if not clean:
        return None
    return float(pd.Series(clean).median())


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if pd.isna(numerator) or pd.isna(denominator) or float(denominator) == 0:
        return None
    return float(numerator) / float(denominator)


def _member_snapshot(df: pd.DataFrame) -> dict | None:
    if df is None or df.empty:
        return None
    s = df.sort_values("date").reset_index(drop=True).copy()
    close = pd.to_numeric(s.get("close"), errors="coerce")
    high = pd.to_numeric(s.get("high"), errors="coerce")
    low = pd.to_numeric(s.get("low"), errors="coerce")
    volume = pd.to_numeric(s.get("volume"), errors="coerce")
    if "amount" in s.columns:
        amount = pd.to_numeric(s.get("amount"), errors="coerce")
    else:
        amount = close * volume
    if len(close.dropna()) < 40:
        return None

    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    vol_ma20 = volume.rolling(20).mean()
    pct = close.pct_change() * 100.0
    prev_close = close.shift(1)
    amplitude = (high - low) / prev_close.replace(0, pd.NA) * 100.0
    close_pos = ((close - low) / (high - low).replace(0, pd.NA) * 100.0).clip(0, 100).fillna(50.0)

    recent = pd.DataFrame(
        {
            "pct": pct,
            "vol_ratio": volume / vol_ma20.replace(0, pd.NA),
            "amplitude": amplitude,
            "close_pos": close_pos,
            "close": close,
            "ma20": ma20,
            "ma50": ma50,
            "amount": amount,
        }
    ).tail(3)
    if recent.empty:
        return None

    recent_amount_mean = pd.to_numeric(recent["amount"], errors="coerce").dropna().mean()
    base_amount_slice = pd.to_numeric(amount.iloc[-23:-3], errors="coerce").dropna()
    if len(base_amount_slice) < 5:
        base_amount_slice = pd.to_numeric(amount.tail(20), errors="coerce").dropna()
    base_amount_mean = base_amount_slice.mean() if not base_amount_slice.empty else None
    amount_ratio_3d = _safe_ratio(recent_amount_mean, base_amount_mean)

    last_close = float(close.iloc[-1])
    last_ma20 = ma20.iloc[-1] if len(ma20) else pd.NA
    last_ma50 = ma50.iloc[-1] if len(ma50) else pd.NA
    above_ma20 = bool(pd.notna(last_ma20) and float(last_ma20) > 0 and last_close >= float(last_ma20) * 0.99)
    above_ma50 = bool(pd.notna(last_ma50) and float(last_ma50) > 0 and last_close >= float(last_ma50) * 0.99)

    climax_days = recent[
        (pd.to_numeric(recent["pct"], errors="coerce") >= 4.0)
        & (pd.to_numeric(recent["vol_ratio"], errors="coerce") >= 1.5)
        & (pd.to_numeric(recent["close_pos"], errors="coerce") >= 65.0)
        & (pd.to_numeric(recent["amplitude"], errors="coerce") >= 4.0)
    ]
    climax_flag = not climax_days.empty

    ret3 = _safe_return(close, 3)
    ret10 = _safe_return(close, 10)
    breakdown_flag = bool(
        ret3 is not None and ret3 <= -2.0 and amount_ratio_3d is not None and amount_ratio_3d >= 1.05 and not above_ma20
    )
    pullback_shrink_flag = bool(
        ret10 is not None
        and ret10 >= 4.0
        and ret3 is not None
        and ret3 <= -0.5
        and amount_ratio_3d is not None
        and amount_ratio_3d <= 0.95
        and (above_ma20 or above_ma50)
        and not breakdown_flag
    )

    last_pct = pd.to_numeric(recent["pct"], errors="coerce").iloc[-1]
    return {
        "ret_3d": ret3,
        "ret_10d": ret10,
        "amount_ratio_3d": amount_ratio_3d,
        "above_ma50": above_ma50,
        "above_ma20": above_ma20,
        "climax_flag": climax_flag,
        "pullback_shrink_flag": pullback_shrink_flag,
        "breakdown_flag": breakdown_flag,
        "last_pct": float(last_pct) if pd.notna(last_pct) else None,
    }


def _fmt_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value):+.1f}%"


def _fmt_ratio(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value):.2f}x"


def _build_sector_note(info: dict) -> str:
    return (
        f"10日{_fmt_pct(info.get('ret_10d'))}，3日{_fmt_pct(info.get('ret_3d'))}，"
        f"近3日成交额{_fmt_ratio(info.get('amount_ratio_3d'))}，"
        f"上涨占比{float(info.get('breadth_up_pct', 0.0)):.0f}%，"
        f"站上MA50占比{float(info.get('above_ma50_pct', 0.0)):.0f}%"
    )


def _classify_sector_state(info: dict) -> str:
    count = int(info.get("stock_count", 0) or 0)
    ret_10d = info.get("ret_10d")
    ret_3d = info.get("ret_3d")
    amount_ratio_3d = info.get("amount_ratio_3d")
    above_ma50_pct = float(info.get("above_ma50_pct", 0.0) or 0.0)
    climax_pct = float(info.get("climax_pct", 0.0) or 0.0)
    pullback_pct = float(info.get("pullback_shrink_pct", 0.0) or 0.0)
    breakdown_pct = float(info.get("breakdown_pct", 0.0) or 0.0)

    if count < 3:
        return "NEUTRAL_MIXED"
    if (
        ret_10d is not None
        and ret_10d >= 8.0
        and ret_3d is not None
        and ret_3d >= 2.0
        and amount_ratio_3d is not None
        and amount_ratio_3d >= 1.10
        and climax_pct >= 20.0
    ):
        return "CONSENSUS_CLIMAX"
    if (
        ret_10d is not None
        and ret_10d >= 4.0
        and ret_3d is not None
        and ret_3d <= -0.8
        and amount_ratio_3d is not None
        and amount_ratio_3d <= 0.95
        and pullback_pct >= 20.0
        and above_ma50_pct >= 40.0
    ):
        return "DISAGREEMENT_PULLBACK"
    if (
        ret_10d is not None
        and ret_10d <= 1.0
        and ret_3d is not None
        and ret_3d <= -1.5
        and amount_ratio_3d is not None
        and amount_ratio_3d >= 1.05
        and breakdown_pct >= 20.0
    ):
        return "DISTRIBUTION_RISK"
    if ret_10d is not None and ret_10d >= 3.0 and above_ma50_pct >= 45.0 and breakdown_pct < 25.0:
        return "HEALTHY_MAINLINE"
    return "NEUTRAL_MIXED"


def _rotation_score(info: dict) -> float:
    return (
        float(info.get("ret_10d") or 0.0) * 0.7
        + float(info.get("ret_3d") or 0.0) * 0.3
        + float(info.get("above_ma50_pct") or 0.0) * 0.05
        + float(info.get("pullback_shrink_pct") or 0.0) * 0.06
        + float(info.get("climax_pct") or 0.0) * 0.04
        - float(info.get("breakdown_pct") or 0.0) * 0.08
    )


def _group_overview_lines(state_map: dict[str, dict], focus_sectors: list[str] | None = None) -> list[str]:
    focus_set = {str(x).strip() for x in (focus_sectors or []) if str(x).strip()}
    lines: list[str] = []
    for state in _OVERVIEW_STATE_ORDER:
        bucket = [(sec, info) for sec, info in state_map.items() if str(info.get("state", "")) == state]
        if not bucket:
            continue
        bucket = sorted(
            bucket,
            key=lambda item: (
                0 if item[0] in focus_set else 1,
                -float(item[1].get("rotation_score", 0.0) or 0.0),
                item[0],
            ),
        )[:3]
        joined = "； ".join(
            f"{sector}(10日{_fmt_pct(info.get('ret_10d'))}, 3日{_fmt_pct(info.get('ret_3d'))}, 量{_fmt_ratio(info.get('amount_ratio_3d'))})"
            for sector, info in bucket
        )
        if joined:
            lines.append(f"{SECTOR_STATE_LABELS.get(state, state)}: {joined}")
    return lines


def analyze_sector_rotation(
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    *,
    universe_symbols: list[str] | None = None,
    focus_sectors: list[str] | None = None,
) -> dict[str, object]:
    symbols = universe_symbols or list(df_map.keys())
    grouped: dict[str, list[str]] = {}
    for code in symbols:
        sector = str(sector_map.get(code, "") or "").strip()
        if not sector:
            continue
        grouped.setdefault(sector, []).append(code)

    state_map: dict[str, dict] = {}
    counts: dict[str, int] = dict.fromkeys(SECTOR_STATE_LABELS.keys(), 0)
    for sector, members in grouped.items():
        snapshots = []
        for code in members:
            snap = _member_snapshot(df_map.get(code))
            if snap:
                snapshots.append(snap)
        if not snapshots:
            continue

        stock_count = len(snapshots)
        breadth_up_pct = sum(1 for x in snapshots if (x.get("last_pct") or 0.0) > 0) / stock_count * 100.0
        above_ma50_pct = sum(1 for x in snapshots if bool(x.get("above_ma50"))) / stock_count * 100.0
        climax_pct = sum(1 for x in snapshots if bool(x.get("climax_flag"))) / stock_count * 100.0
        pullback_pct = sum(1 for x in snapshots if bool(x.get("pullback_shrink_flag"))) / stock_count * 100.0
        breakdown_pct = sum(1 for x in snapshots if bool(x.get("breakdown_flag"))) / stock_count * 100.0

        info = {
            "stock_count": stock_count,
            "ret_3d": _safe_median(x.get("ret_3d") for x in snapshots),
            "ret_10d": _safe_median(x.get("ret_10d") for x in snapshots),
            "amount_ratio_3d": _safe_median(x.get("amount_ratio_3d") for x in snapshots),
            "breadth_up_pct": breadth_up_pct,
            "above_ma50_pct": above_ma50_pct,
            "climax_pct": climax_pct,
            "pullback_shrink_pct": pullback_pct,
            "breakdown_pct": breakdown_pct,
        }
        state = _classify_sector_state(info)
        info["state"] = state
        info["label"] = SECTOR_STATE_LABELS.get(state, state)
        info["guidance"] = SECTOR_STATE_GUIDANCE.get(state, "")
        info["rotation_score"] = _rotation_score(info)
        info["note"] = _build_sector_note(info)
        state_map[sector] = info
        counts[state] = counts.get(state, 0) + 1

    headline = (
        f"分歧{counts.get('DISAGREEMENT_PULLBACK', 0)} | "
        f"健康{counts.get('HEALTHY_MAINLINE', 0)} | "
        f"高潮{counts.get('CONSENSUS_CLIMAX', 0)} | "
        f"退潮{counts.get('DISTRIBUTION_RISK', 0)} | "
        f"中性{counts.get('NEUTRAL_MIXED', 0)}"
    )
    return {
        "state_map": state_map,
        "counts": counts,
        "headline": headline,
        "overview_lines": _group_overview_lines(state_map, focus_sectors=focus_sectors),
    }
