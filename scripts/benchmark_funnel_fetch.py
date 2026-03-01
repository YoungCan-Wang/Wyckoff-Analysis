# -*- coding: utf-8 -*-
"""
Funnel 取数基准脚本

用途：
1) 先测吞吐再调参（workers / mode）
2) 对比 serial / thread / process 模式性能

示例：
python -m scripts.benchmark_funnel_fetch --symbols 600519,000001,300750 --mode thread --workers 12
python -m scripts.benchmark_funnel_fetch --sample 400 --mode process --workers 8
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.fetch_a_share_csv import _normalize_symbols, _resolve_trading_window, get_stocks_by_board
from core.wyckoff_engine import normalize_hist_from_fetch

CN_TZ = ZoneInfo("Asia/Shanghai")


def _job_end_calendar_day(close_hour: int = 15) -> date:
    now = datetime.now(CN_TZ)
    if now.hour >= close_hour:
        return now.date()
    return (now - timedelta(days=1)).date()


def _fetch_one(symbol: str, window) -> tuple[str, bool]:
    from integrations.fetch_a_share_csv import _fetch_hist

    try:
        raw = _fetch_hist(symbol=symbol, window=window, adjust="qfq")
        df = normalize_hist_from_fetch(raw)
        ok = df is not None and not df.empty
        return (symbol, bool(ok))
    except Exception:
        return (symbol, False)


def _build_universe(sample: int) -> list[str]:
    main = [str(x.get("code", "")).strip() for x in get_stocks_by_board("main")]
    chinext = [str(x.get("code", "")).strip() for x in get_stocks_by_board("chinext")]
    merged = _normalize_symbols(main + chinext)
    return merged[: max(sample, 1)]


def _run_serial(symbols: list[str], window) -> tuple[int, int, float]:
    ok = 0
    fail = 0
    t0 = time.monotonic()
    for sym in symbols:
        _, s_ok = _fetch_one(sym, window)
        if s_ok:
            ok += 1
        else:
            fail += 1
    elapsed = time.monotonic() - t0
    return (ok, fail, elapsed)


def _run_pool(symbols: list[str], window, mode: str, workers: int) -> tuple[int, int, float]:
    ok = 0
    fail = 0
    t0 = time.monotonic()
    ex_cls = ProcessPoolExecutor if mode == "process" else ThreadPoolExecutor
    with ex_cls(max_workers=max(workers, 1)) as ex:
        futures = [ex.submit(_fetch_one, sym, window) for sym in symbols]
        for fut in as_completed(futures):
            _, s_ok = fut.result()
            if s_ok:
                ok += 1
            else:
                fail += 1
    elapsed = time.monotonic() - t0
    return (ok, fail, elapsed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wyckoff Funnel 取数基准测试")
    parser.add_argument("--symbols", default="", help="逗号分隔股票代码，优先使用")
    parser.add_argument("--sample", type=int, default=200, help="未指定 symbols 时，从股票池取前 N 只")
    parser.add_argument("--workers", type=int, default=8, help="并发 worker 数")
    parser.add_argument("--mode", choices=["serial", "thread", "process"], default="thread")
    parser.add_argument("--trading-days", type=int, default=500)
    args = parser.parse_args()

    if args.symbols.strip():
        symbols = _normalize_symbols([x.strip() for x in args.symbols.split(",") if x.strip()])
    else:
        symbols = _build_universe(args.sample)

    if not symbols:
        print("[bench] 无有效股票代码")
        return 1

    window = _resolve_trading_window(
        end_calendar_day=_job_end_calendar_day(),
        trading_days=max(args.trading_days, 30),
    )

    print(
        f"[bench] mode={args.mode}, workers={args.workers}, symbols={len(symbols)}, "
        f"window={window.start_trade_date}->{window.end_trade_date}"
    )
    if args.mode == "serial":
        ok, fail, elapsed = _run_serial(symbols, window)
    else:
        ok, fail, elapsed = _run_pool(symbols, window, args.mode, args.workers)

    qps = ok / elapsed if elapsed > 0 else 0.0
    avg = elapsed / len(symbols) if symbols else 0.0
    print(
        f"[bench] done: ok={ok}, fail={fail}, elapsed={elapsed:.2f}s, "
        f"avg_per_symbol={avg:.3f}s, qps={qps:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
