"""Run V1 and V2 Wyckoff funnels side by side and save a shadow report.

This script is deliberately read-only with respect to recommendations: it does
not send candidates to AI reports or OMS.  It fetches data once through the
current production funnel, then runs the V2 structure strategy on the same
dataset for comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.strategy_compare import (  # noqa: E402
    STRATEGY_V1_CURRENT,
    STRATEGY_V2_STRUCTURE,
    StrategyRun,
    compare_strategy_runs,
    extract_l4_candidates,
    format_strategy_comparison_markdown,
)
from core.wyckoff_engine import FunnelResult  # noqa: E402
from core.wyckoff_v2_structure import run_structure_funnel  # noqa: E402
from scripts.wyckoff_funnel import run_funnel_job  # noqa: E402
from utils.trading_clock import CN_TZ  # noqa: E402


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _build_v1_result(triggers: dict[str, list[tuple[str, float]]], metrics: dict, debug: dict) -> FunnelResult:
    stage_map = dict(metrics.get("accum_stage_map", {}) or {})
    for code in metrics.get("markup_symbols", []) or []:
        stage_map[str(code)] = "Markup"
    return FunnelResult(
        layer1_symbols=list(debug.get("layer1_symbols", []) or []),
        layer2_symbols=list(debug.get("layer2_symbols", []) or []),
        layer3_symbols=list(debug.get("layer3_symbols_raw", []) or []),
        top_sectors=list(metrics.get("top_sectors", []) or []),
        triggers=triggers,
        stage_map=stage_map,
        markup_symbols=list(metrics.get("markup_symbols", []) or []),
        exit_signals=dict(metrics.get("exit_signals", {}) or {}),
        channel_map=dict(metrics.get("layer2_channel_map", {}) or {}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Wyckoff V1/V2 shadow strategy comparison")
    parser.add_argument(
        "--output-dir",
        default="data/strategy_shadow",
        help="Directory for markdown/json comparison artifacts",
    )
    parser.add_argument(
        "--send-feishu",
        action="store_true",
        help="Send the markdown comparison report to FEISHU_WEBHOOK_URL after artifacts are written.",
    )
    args = parser.parse_args(argv)

    triggers, metrics = run_funnel_job(include_debug_context=True)
    debug = metrics.get("_debug", {}) or {}
    required = ["cfg", "all_symbols", "all_df_map", "name_map", "market_cap_map", "sector_map", "bench_df"]
    missing = [key for key in required if key not in debug]
    if missing:
        raise RuntimeError(f"run_funnel_job did not return debug keys: {missing}")

    v1_result = _build_v1_result(triggers, metrics, debug)
    v2_result = run_structure_funnel(
        all_symbols=list(debug["all_symbols"]),
        df_map=dict(debug["all_df_map"]),
        bench_df=debug["bench_df"],
        name_map=dict(debug["name_map"]),
        market_cap_map=dict(debug["market_cap_map"]),
        sector_map=dict(debug["sector_map"]),
        cfg=debug["cfg"],
        financial_map=metrics.get("financial_map", {}) or {},
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
    report = format_strategy_comparison_markdown(
        (v1_run, v2_run),
        comparison,
        name_map=debug["name_map"],
        sector_map=debug["sector_map"],
        benchmark_context=metrics.get("benchmark_context", {}) or {},
        input_symbol_count=int(metrics.get("total_symbols", len(debug["all_symbols"])) or 0),
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"wyckoff_v2_shadow_{ts}.md"
    json_path = out_dir / f"wyckoff_v2_shadow_{ts}.json"

    md_path.write_text(report, encoding="utf-8")
    payload = {
        "generated_at": datetime.now(CN_TZ).isoformat(),
        "strategy_ids": list(comparison.strategy_ids),
        "counts": comparison.counts,
        "intersection": list(comparison.intersection),
        "only_by_strategy": {key: list(value) for key, value in comparison.only_by_strategy.items()},
        "runs": {
            v1_run.strategy_id: [asdict(candidate) for candidate in v1_run.candidates],
            v2_run.strategy_id: [asdict(candidate) for candidate in v2_run.candidates],
        },
        "benchmark_context": metrics.get("benchmark_context", {}) or {},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(f"[shadow] markdown: {md_path}")
    print(f"[shadow] json: {json_path}")
    if args.send_feishu:
        webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        if not webhook:
            print("[shadow] FEISHU_WEBHOOK_URL 未配置，无法发送影子策略报告")
            return 1
        from utils.feishu import send_feishu_notification  # noqa: E402

        title = f"🔬 Wyckoff V2 结构影子池 {datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M')}"
        ok = send_feishu_notification(webhook, title, report)
        if not ok:
            print("[shadow] 飞书发送失败")
            return 1
        print("[shadow] 飞书发送成功")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
