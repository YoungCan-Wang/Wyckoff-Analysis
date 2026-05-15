"""
Funnel 行情取数基准脚本。

用途：
1) 对比 TickFlow batch vs 逐票回源的性能和成功率。
2) 评估不同 batch_size / workers 配置的吞吐量。

示例：
python -m scripts.benchmark_funnel_fetch --sample 400 --path single
python -m scripts.benchmark_funnel_fetch --sample 0 --path compare --runner batch
python -m scripts.benchmark_funnel_fetch --sample 200 --runner batch --disable-tickflow-batch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.fetch_a_share_csv import _normalize_symbols, _resolve_trading_window, get_stocks_by_board
from utils.trading_clock import resolve_end_calendar_day


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def _latest_date(df: pd.DataFrame | None) -> str:
    if df is None or df.empty or "date" not in df.columns:
        return ""
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    return dates.iloc[-1].date().isoformat() if not dates.empty else ""


def _build_universe(sample: int) -> list[str]:
    main = [str(x.get("code", "")).strip() for x in get_stocks_by_board("main")]
    chinext = [str(x.get("code", "")).strip() for x in get_stocks_by_board("chinext")]
    merged = _normalize_symbols(main + chinext)
    if sample <= 0 or sample >= len(merged):
        return merged
    step = len(merged) / max(sample, 1)
    return [merged[min(int(i * step), len(merged) - 1)] for i in range(sample)]


def _fetch_one(symbol: str, window) -> dict[str, Any]:
    try:
        from integrations.data_source import fetch_stock_hist

        raw = fetch_stock_hist(symbol=symbol, start=window.start_trade_date, end=window.end_trade_date, adjust="qfq")
        df = normalize_hist_from_fetch(raw)
        return {
            "symbol": symbol,
            "ok": bool(df is not None and not df.empty),
            "latest": _latest_date(df),
            "source": str(raw.attrs.get("source", "") or ""),
            "upstream_source": str(raw.attrs.get("upstream_source", "") or ""),
        }
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": type(e).__name__}


def _summarize(label: str, symbols: list[str], rows: list[dict[str, Any]], elapsed: float, target_date: str) -> dict:
    ok = sum(1 for row in rows if row.get("ok"))
    aligned = sum(1 for row in rows if row.get("ok") and row.get("latest") == target_date)
    summary = {
        "path": label,
        "symbols": len(symbols),
        "ok": ok,
        "fail": len(symbols) - ok,
        "success_pct": round(ok / len(symbols) * 100, 2) if symbols else 0.0,
        "aligned": aligned,
        "aligned_pct": round(aligned / len(symbols) * 100, 2) if symbols else 0.0,
        "elapsed_s": round(elapsed, 2),
        "avg_ms": round(elapsed / len(symbols) * 1000, 1) if symbols else 0.0,
        "qps": round(ok / elapsed, 3) if elapsed > 0 else 0.0,
        "sources": dict(Counter(str(row.get("source", "") or "unknown") for row in rows if row.get("ok"))),
        "errors": dict(Counter(str(row.get("error", "") or "-") for row in rows if not row.get("ok"))),
    }
    return summary


def _run_single(symbols: list[str], window, args) -> tuple[list[dict[str, Any]], float]:
    started = time.monotonic()
    if args.mode == "serial":
        rows = [_fetch_one(sym, window) for sym in symbols]
        return rows, time.monotonic() - started
    ex_cls = ProcessPoolExecutor if args.mode == "process" else ThreadPoolExecutor
    with ex_cls(max_workers=max(args.workers, 1)) as ex:
        futures = [ex.submit(_fetch_one, sym, window) for sym in symbols]
        rows = [fut.result() for fut in as_completed(futures)]
    return rows, time.monotonic() - started


def _run_batch(symbols: list[str], window, args) -> tuple[list[dict[str, Any]], float, dict]:
    import tools.data_fetcher as dfetcher

    if args.disable_tickflow_batch:
        dfetcher.TICKFLOW_BATCH_ENABLED = False
    started = time.monotonic()
    df_map, stats = dfetcher.fetch_all_ohlcv(
        symbols=symbols,
        window=window,
        enforce_target_trade_date=args.enforce_target_date,
        batch_size=args.batch_size,
        max_workers=args.workers,
        batch_timeout=args.batch_timeout,
        batch_sleep=args.batch_sleep,
        executor_mode=args.mode if args.mode != "serial" else "thread",
    )
    rows = [
        {
            "symbol": sym,
            "ok": sym in df_map and df_map[sym] is not None and not df_map[sym].empty,
            "latest": _latest_date(df_map.get(sym)),
            "source": str((df_map.get(sym).attrs if sym in df_map else {}).get("source", "") or ""),
        }
        for sym in symbols
    ]
    return rows, time.monotonic() - started, stats


def _run_path(label: str, symbols: list[str], window, args, *, runner_override: str = "") -> dict:
    runner = runner_override or args.runner
    print(f"[bench] start path={label}, runner={runner}, symbols={len(symbols)}")
    if runner == "batch":
        rows, elapsed, stats = _run_batch(symbols, window, args)
    else:
        rows, elapsed = _run_single(symbols, window, args)
        stats = {}
    summary = _summarize(label, symbols, rows, elapsed, window.end_trade_date.isoformat())
    summary["fetch_stats"] = stats
    print(
        f"[bench] {label}: ok={summary['ok']}/{summary['symbols']} "
        f"success={summary['success_pct']}% aligned={summary['aligned_pct']}% "
        f"elapsed={summary['elapsed_s']}s avg={summary['avg_ms']}ms qps={summary['qps']}"
    )
    print(f"[bench] {label}: sources={summary['sources']}")
    if summary["errors"]:
        print(f"[bench] {label}: errors={summary['errors']}")
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wyckoff Funnel 行情取数基准测试")
    parser.add_argument("--symbols", default="", help="逗号分隔股票代码，优先使用")
    parser.add_argument("--sample", type=int, default=200, help="未指定 symbols 时取样；0 表示全量")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mode", choices=["serial", "thread", "process"], default="thread")
    parser.add_argument("--runner", choices=["batch", "single"], default="batch")
    parser.add_argument("--path", choices=["batch", "single", "compare"], default="compare")
    parser.add_argument("--trading-days", type=int, default=320)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--batch-timeout", type=int, default=420)
    parser.add_argument("--batch-sleep", type=float, default=0.55)
    parser.add_argument("--disable-tickflow-batch", action="store_true")
    parser.add_argument("--enforce-target-date", action="store_true")
    parser.add_argument("--output", default="", help="可选 JSON 输出路径")
    return parser.parse_args()


def main() -> int:
    _load_dotenv()
    args = _parse_args()
    symbols = (
        _normalize_symbols([x.strip() for x in args.symbols.split(",") if x.strip()])
        if args.symbols.strip()
        else _build_universe(args.sample)
    )
    if not symbols:
        print("[bench] 无有效股票代码")
        return 1
    window = _resolve_trading_window(resolve_end_calendar_day(), max(args.trading_days, 30))
    print(
        f"[bench] runner={args.runner}, mode={args.mode}, workers={args.workers}, "
        f"symbols={len(symbols)}, window={window.start_trade_date}->{window.end_trade_date}, "
        f"disable_tickflow_batch={args.disable_tickflow_batch}"
    )
    if args.path == "compare":
        summaries = [
            _run_path("batch", symbols, window, args, runner_override="batch"),
            _run_path("single", symbols, window, args, runner_override="single"),
        ]
    else:
        summaries = [_run_path(args.path, symbols, window, args, runner_override=args.path)]
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
