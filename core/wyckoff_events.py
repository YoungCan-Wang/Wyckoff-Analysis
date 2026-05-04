"""Wyckoff signal-to-event classification.

This borrows CZSC's Signal -> Event mindset without importing Chan theory
objects.  Wyckoff remains the domain language; this module only turns raw
trigger facts into readable, testable event labels.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WyckoffEvent:
    event_id: str
    label: str
    track: str
    action: str
    confidence: str
    reasons: tuple[str, ...]
    watch_points: tuple[str, ...]


def _norm_set(values: tuple[str, ...] | list[str] | set[str]) -> set[str]:
    return {str(x or "").strip().lower() for x in values if str(x or "").strip()}


def classify_wyckoff_event(
    triggers: tuple[str, ...] | list[str] | set[str],
    *,
    stage: str = "",
    channel: str = "",
    score: float = 0.0,
    regime: str = "",
) -> WyckoffEvent:
    """Classify raw Wyckoff triggers into a readable event.

    The returned action is deliberately phrased as observation, not a trade
    instruction.  Trading decisions can later consume these event ids.
    """

    trigger_set = _norm_set(triggers)
    stage_s = str(stage or "").strip()
    channel_s = str(channel or "").strip()
    regime_s = str(regime or "").strip().upper()
    reasons: list[str] = []
    if stage_s:
        reasons.append(f"阶段={stage_s}")
    if channel_s:
        reasons.append(f"通道={channel_s}")
    if regime_s:
        reasons.append(f"水温={regime_s}")
    if score:
        reasons.append(f"分数={float(score):.2f}")

    if "sos" in trigger_set and stage_s == "Markup":
        return WyckoffEvent(
            event_id="right_side_ignition",
            label="右侧点火",
            track="Trend",
            action="强度观察",
            confidence="high" if score >= 10 else "medium",
            reasons=tuple(reasons + ["SOS 放量突破", "Markup 主升阶段"]),
            watch_points=("次日不宜大幅回落至突破位下方", "量能需要维持或温和缩量承接"),
        )

    if "spring" in trigger_set and ("lps" in trigger_set or "evr" in trigger_set):
        return WyckoffEvent(
            event_id="accumulation_repair_resonance",
            label="吸筹修复共振",
            track="Accum",
            action="低位观察",
            confidence="high" if score >= 5 else "medium",
            reasons=tuple(reasons + ["Spring 修复", "缩量回踩或放量承接共振"]),
            watch_points=("不能再次有效跌破交易区间下沿", "后续需要从修复转向放量上攻"),
        )

    if "spring" in trigger_set:
        return WyckoffEvent(
            event_id="spring_reclaim",
            label="Spring 修复",
            track="Accum",
            action="低位观察",
            confidence="medium",
            reasons=tuple(reasons + ["假跌破后重新收回"]),
            watch_points=("观察是否站回区间内部", "避免次日继续破位"),
        )

    if "lps" in trigger_set:
        return WyckoffEvent(
            event_id="lps_pullback_confirm",
            label="LPS 回踩确认",
            track="Accum",
            action="支撑观察",
            confidence="medium",
            reasons=tuple(reasons + ["缩量回踩支撑"]),
            watch_points=("回踩不应放量跌破支撑", "后续需要重新转强"),
        )

    if "evr" in trigger_set:
        return WyckoffEvent(
            event_id="volume_absorption",
            label="放量承接",
            track="Trend" if stage_s == "Markup" else "Accum",
            action="承接观察",
            confidence="medium",
            reasons=tuple(reasons + ["放量但价格不弱"]),
            watch_points=("放量后不能快速转弱", "等待后续转为 SOS 或 LPS 确认"),
        )

    if "sos" in trigger_set:
        return WyckoffEvent(
            event_id="sos_watch",
            label="SOS 观察",
            track="Trend",
            action="强度观察",
            confidence="medium",
            reasons=tuple(reasons + ["SOS 信号"]),
            watch_points=("确认突破是否有效", "高开过多不宜追"),
        )

    return WyckoffEvent(
        event_id="wyckoff_watch",
        label="威科夫观察",
        track="Watch",
        action="观察",
        confidence="low",
        reasons=tuple(reasons),
        watch_points=("等待更多结构确认",),
    )


__all__ = ["WyckoffEvent", "classify_wyckoff_event"]
