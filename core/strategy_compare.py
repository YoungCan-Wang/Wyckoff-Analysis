"""Helpers for running and comparing shadow stock-selection strategies."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.wyckoff_engine import FunnelConfig, FunnelResult, run_funnel
from core.wyckoff_v2_structure import STRATEGY_ID as STRATEGY_V2_STRUCTURE
from core.wyckoff_v2_structure import run_structure_funnel

STRATEGY_V1_CURRENT = "wyckoff_v1_current"

TRIGGER_LABELS = {
    "sos": "SOS（强势信号）",
    "spring": "Spring（假跌破修复）",
    "lps": "LPS（最后支撑点）",
    "evr": "EVR（放量不跌）",
}

TRIGGER_GROUP_ORDER = ("sos", "spring", "lps", "evr")

TRIGGER_GROUP_TITLES = {
    "sos": "⚡ SOS（强势信号） 量价点火",
    "spring": "🌱 Spring（假跌破修复） 终极震仓",
    "lps": "🔄 LPS（最后支撑点） 缩量回踩",
    "evr": "📊 EVR（放量不跌） 放量不跌",
}

STAGE_LABELS = {
    "Markup": "Markup（主升阶段）",
    "Accum_A": "Accum_A（吸筹A段）",
    "Accum_B": "Accum_B（吸筹B段）",
    "Accum_C": "Accum_C（吸筹C段/确认期）",
}


@dataclass(frozen=True)
class StrategyCandidate:
    strategy_id: str
    code: str
    triggers: tuple[str, ...]
    score: float
    stage: str = ""
    channel: str = ""


@dataclass(frozen=True)
class StrategyRun:
    strategy_id: str
    result: FunnelResult
    candidates: tuple[StrategyCandidate, ...]


@dataclass(frozen=True)
class StrategyComparison:
    strategy_ids: tuple[str, ...]
    intersection: tuple[str, ...]
    union: tuple[str, ...]
    only_by_strategy: dict[str, tuple[str, ...]]
    counts: dict[str, int]


def extract_l4_candidates(result: FunnelResult, strategy_id: str) -> tuple[StrategyCandidate, ...]:
    """Flatten a FunnelResult's L4 triggers into comparable candidates."""

    trigger_map: dict[str, list[str]] = {}
    score_map: dict[str, float] = {}
    for trigger_key, pairs in result.triggers.items():
        for code, score in pairs:
            code_s = str(code).strip()
            if not code_s:
                continue
            trigger_map.setdefault(code_s, []).append(str(trigger_key))
            score_map[code_s] = score_map.get(code_s, 0.0) + float(score)

    out: list[StrategyCandidate] = []
    for code in sorted(trigger_map, key=lambda c: (-score_map.get(c, 0.0), c)):
        out.append(
            StrategyCandidate(
                strategy_id=strategy_id,
                code=code,
                triggers=tuple(trigger_map.get(code, [])),
                score=float(score_map.get(code, 0.0)),
                stage=str(result.stage_map.get(code, "") or ""),
                channel=str(result.channel_map.get(code, "") or ""),
            )
        )
    return tuple(out)


def compare_strategy_runs(runs: list[StrategyRun] | tuple[StrategyRun, ...]) -> StrategyComparison:
    """Compare candidate sets across strategy runs."""

    if not runs:
        return StrategyComparison(strategy_ids=(), intersection=(), union=(), only_by_strategy={}, counts={})

    strategy_ids = tuple(run.strategy_id for run in runs)
    code_sets = {run.strategy_id: {candidate.code for candidate in run.candidates} for run in runs}
    all_codes = set().union(*code_sets.values()) if code_sets else set()
    intersection = set.intersection(*code_sets.values()) if code_sets else set()
    only_by_strategy: dict[str, tuple[str, ...]] = {}
    for strategy_id, codes in code_sets.items():
        other_codes = set().union(*(v for k, v in code_sets.items() if k != strategy_id))
        only_by_strategy[strategy_id] = tuple(sorted(codes - other_codes))

    counts = {
        "union": len(all_codes),
        "intersection": len(intersection),
    }
    for strategy_id, codes in code_sets.items():
        counts[strategy_id] = len(codes)
        counts[f"only_{strategy_id}"] = len(only_by_strategy[strategy_id])

    return StrategyComparison(
        strategy_ids=strategy_ids,
        intersection=tuple(sorted(intersection)),
        union=tuple(sorted(all_codes)),
        only_by_strategy=only_by_strategy,
        counts=counts,
    )


def format_strategy_comparison_markdown(
    runs: list[StrategyRun] | tuple[StrategyRun, ...],
    comparison: StrategyComparison,
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    benchmark_context: dict | None = None,
    input_symbol_count: int | None = None,
) -> str:
    """Render a standalone Wyckoff-style report for the V2 shadow strategy."""

    name_map = name_map or {}
    sector_map = sector_map or {}
    benchmark_context = benchmark_context or {}
    shadow_run = next((run for run in runs if run.strategy_id == STRATEGY_V2_STRUCTURE), runs[-1] if runs else None)

    def _trigger_text(candidate: StrategyCandidate | None) -> str:
        if candidate is None or not candidate.triggers:
            return "-"
        return "+".join(TRIGGER_LABELS.get(trigger, trigger) for trigger in candidate.triggers)

    def _stage_text(stage: str) -> str:
        stage_s = str(stage or "").strip()
        return STAGE_LABELS.get(stage_s, stage_s or "-")

    def _score_star(score: float) -> str:
        if score >= 10:
            return "★★"
        if score >= 5:
            return "★ "
        return "  "

    def _fmt_float(value, digits: int = 2, suffix: str = "") -> str:
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except Exception:
            return "-"

    shadow_candidates = list(shadow_run.candidates if shadow_run is not None else ())
    shadow_candidates = sorted(shadow_candidates, key=lambda c: (-c.score, c.code))
    shadow_codes = {candidate.code for candidate in shadow_candidates}
    event_count = sum(len(candidate.triggers) for candidate in shadow_candidates)
    scope_text = str(input_symbol_count) if input_symbol_count is not None else "当前股票池"
    regime = str(benchmark_context.get("regime", "") or "-")
    close = benchmark_context.get("close")
    ma50 = benchmark_context.get("ma50")
    ma200 = benchmark_context.get("ma200")
    recent3 = benchmark_context.get("recent3_cum_pct")
    breadth = benchmark_context.get("breadth", {}) or {}
    breadth_ratio = breadth.get("ratio_pct") if isinstance(breadth, dict) else None

    bench_parts = [regime]
    if close is not None:
        bench_parts.append(f"收盘 {_fmt_float(close, 2)}")
    if ma50 is not None:
        bench_parts.append(f"MA50 {_fmt_float(ma50, 2)}")
    if ma200 is not None:
        bench_parts.append(f"MA200 {_fmt_float(ma200, 2)}")
    if recent3 is not None:
        bench_parts.append(f"近3日 {_fmt_float(recent3, 2, '%')}")
    if breadth_ratio is not None:
        bench_parts.append(f"广度 {_fmt_float(breadth_ratio, 1, '%')}")
    bench_line = " | ".join(bench_parts)

    sector_counts: dict[str, int] = {}
    for candidate in shadow_candidates:
        industry = str(sector_map.get(candidate.code, "") or "未知行业").strip()
        sector_counts[industry] = sector_counts.get(industry, 0) + 1
    top_sectors = [sector for sector, _ in sorted(sector_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]

    trigger_counts = {key: 0 for key in TRIGGER_GROUP_ORDER}
    for candidate in shadow_candidates:
        for trigger in candidate.triggers:
            if trigger in trigger_counts:
                trigger_counts[trigger] += 1

    lines = [
        f"**股票池**: 影子池 {scope_text} 只",
        f"**漏斗概览**: {scope_text}只 → 结构命中:{len(shadow_codes)}（信号事件 {event_count} 次）",
        f"**大盘水温**: {bench_line}",
        f"**Top 行业**: {', '.join(top_sectors) if top_sectors else '无'}",
        f"**L4 触发**: SOS:{trigger_counts['sos']} | Spring:{trigger_counts['spring']} | LPS:{trigger_counts['lps']} | EVR:{trigger_counts['evr']}",
        "",
    ]

    multi_signal = [candidate for candidate in shadow_candidates if len(candidate.triggers) > 1]
    if multi_signal:
        lines.append(f"**【🔥 多信号共振】{len(multi_signal)} 只**")
        for candidate in multi_signal:
            lines.append(
                f"{_score_star(candidate.score)} {candidate.code} {name_map.get(candidate.code, candidate.code)}  "
                f"{candidate.score:.2f}  {_trigger_text(candidate)}  [{_stage_text(candidate.stage)}]"
            )
        lines.append("")

    multi_codes = {candidate.code for candidate in multi_signal}
    single_signal = [candidate for candidate in shadow_candidates if candidate.code not in multi_codes]
    for group_key in TRIGGER_GROUP_ORDER:
        group_candidates = [
            candidate
            for candidate in single_signal
            if (candidate.triggers[0] if candidate.triggers else "") == group_key
        ]
        if not group_candidates:
            continue
        lines.append(f"**【{TRIGGER_GROUP_TITLES.get(group_key, group_key)}】{len(group_candidates)} 只**")
        for candidate in group_candidates:
            industry = str(sector_map.get(candidate.code, "") or "未知行业").strip()
            stage = _stage_text(candidate.stage)
            channel = candidate.channel or "-"
            lines.append(
                f"{_score_star(candidate.score)} {candidate.code} {name_map.get(candidate.code, candidate.code)}  "
                f"{candidate.score:.2f}  [{stage}]  {channel}  [{industry}]"
            )
        lines.append("")

    if not shadow_candidates:
        lines.append("无")

    lines.extend(
        [
            "",
            "**说明**",
            "影子池只用于研究观察，不构成投资建议或交易指令。",
            "分数仅用于本策略内部排序，不代表收益预测。",
        ]
    )

    return "\n".join(lines) + "\n"


def run_shadow_strategy_suite(
    all_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    sector_map: dict[str, str],
    cfg: FunnelConfig | None = None,
) -> tuple[StrategyRun, StrategyRun, StrategyComparison]:
    """Run V1 and V2 on the same inputs and return their comparison."""

    cfg = cfg or FunnelConfig()
    v1_result = run_funnel(
        all_symbols=all_symbols,
        df_map=df_map,
        bench_df=bench_df,
        name_map=name_map,
        market_cap_map=market_cap_map,
        sector_map=sector_map,
        cfg=cfg,
    )
    v2_result = run_structure_funnel(
        all_symbols=all_symbols,
        df_map=df_map,
        bench_df=bench_df,
        name_map=name_map,
        market_cap_map=market_cap_map,
        sector_map=sector_map,
        cfg=cfg,
    )
    v1_run = StrategyRun(
        strategy_id=STRATEGY_V1_CURRENT,
        result=v1_result,
        candidates=extract_l4_candidates(v1_result, STRATEGY_V1_CURRENT),
    )
    v2_run = StrategyRun(
        strategy_id=STRATEGY_V2_STRUCTURE,
        result=v2_result,
        candidates=extract_l4_candidates(v2_result, STRATEGY_V2_STRUCTURE),
    )
    comparison = compare_strategy_runs((v1_run, v2_run))
    return v1_run, v2_run, comparison


__all__ = [
    "STRATEGY_V1_CURRENT",
    "STRATEGY_V2_STRUCTURE",
    "StrategyCandidate",
    "StrategyComparison",
    "StrategyRun",
    "compare_strategy_runs",
    "extract_l4_candidates",
    "format_strategy_comparison_markdown",
    "run_shadow_strategy_suite",
]
