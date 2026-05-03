#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.data_source import fetch_index_hist, fetch_market_cap_map, fetch_sector_map, fetch_stock_hist
from integrations.fetch_a_share_csv import _normalize_symbols, get_stocks_by_board
from integrations.stock_hist_repository import get_stock_hist as get_stock_hist_cached


def _as_yyyymmdd(text: str) -> str:
    return str(text or "").strip().replace("-", "")


def _normalize_board(board: str) -> str:
    b = str(board or "").strip().lower()
    # 回测统一口径：all 等价为主板+创业板
    if b in {"", "all"}:
        return "main_chinext"
    return b


def _load_symbols(board: str, sample_size: int) -> tuple[list[str], list[dict]]:
    board_norm = _normalize_board(board)
    raw_pool = get_stocks_by_board(board_norm)
    pool: list[dict] = []
    for item in raw_pool:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if not code:
            continue
        pool.append({"code": code, "name": name})

    name_map = {
        str(x.get("code", "")).strip(): str(x.get("name", "")).strip() for x in pool if str(x.get("code", "")).strip()
    }
    symbols = [
        s for s in sorted(set(_normalize_symbols(list(name_map.keys())))) if "ST" not in name_map.get(s, "").upper()
    ]
    if sample_size > 0 and sample_size < len(symbols):
        random.seed(42)
        symbols = random.sample(symbols, sample_size)
    filtered_pool = [{"code": s, "name": name_map.get(s, "")} for s in symbols]
    return symbols, filtered_pool


def _bool_env(name: str, default: bool = False) -> bool:
    val = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return val in {"1", "true", "yes", "on"}


def _fetch_one(
    symbol: str,
    prefetch_start: str,
    end_s: str,
    *,
    allow_network_fallback: bool,
) -> tuple[str, pd.DataFrame | None, str | None, float]:
    t0 = time.monotonic()
    try:
        raw = None
        # 1) 缓存优先；cache_only=True 不触发外部数据源补拉，避免单票慢调用拖垮整批任务
        try:
            raw = get_stock_hist_cached(
                symbol,
                prefetch_start,
                end_s,
                adjust="qfq",
                context="background",
                cache_only=True,
            )
        except Exception:
            raw = None

        # 2) 可选网络回退（默认关闭）
        if (raw is None or raw.empty) and allow_network_fallback:
            try:
                raw = fetch_stock_hist(symbol, prefetch_start, end_s, adjust="qfq")
            except Exception:
                raw = None

        if raw is None or raw.empty:
            reason = "cache_miss" if not allow_network_fallback else "no_data"
            return (symbol, None, reason, time.monotonic() - t0)

        df = normalize_hist_from_fetch(raw)
        if df is None or df.empty:
            return (symbol, None, "normalized_empty", time.monotonic() - t0)
        df["symbol"] = symbol
        return (symbol, df, None, time.monotonic() - t0)
    except Exception as e:
        return (symbol, None, str(e), time.monotonic() - t0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest Grid snapshot fetcher")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--board", default="main_chinext")
    parser.add_argument("--sample-size", type=int, default=0)
    parser.add_argument("--trading-days", type=int, default=320)
    parser.add_argument("--output-dir", default="snapshot_data")
    parser.add_argument("--max-workers", type=int, default=int(os.getenv("BACKTEST_SNAPSHOT_WORKERS", "6")))
    parser.add_argument(
        "--allow-network-fallback",
        action="store_true",
        default=_bool_env("BACKTEST_SNAPSHOT_ALLOW_NETWORK_FALLBACK", False),
        help="缓存未命中时允许回退外部数据源（默认关闭）",
    )
    args = parser.parse_args()

    start_s = _as_yyyymmdd(args.start)
    end_s = _as_yyyymmdd(args.end)
    start_dt = datetime.strptime(start_s, "%Y%m%d").date()
    prefetch_start = (start_dt - timedelta(days=int(args.trading_days * 2))).strftime("%Y%m%d")

    print(f"[snapshot] 数据区间: {prefetch_start} -> {end_s}")
    print(
        "[snapshot] fetch模式: "
        f"allow_network_fallback={args.allow_network_fallback}, workers={max(int(args.max_workers), 1)}"
    )

    symbols, raw_pool = _load_symbols(args.board, int(args.sample_size))
    if not symbols:
        print("[snapshot] 严重错误: 股票池为空，请检查 board 参数或行情源可用性")
        return 1
    print(
        f"[snapshot] 股票池: {len(symbols)} symbols, sample={symbols[:5]}, "
        f"board={_normalize_board(args.board)}, exclude_st=True"
    )

    all_frames: list[pd.DataFrame] = []
    ok = 0
    fail = 0
    fail_samples: list[str] = []

    workers = max(int(args.max_workers), 1)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _fetch_one,
                sym,
                prefetch_start,
                end_s,
                allow_network_fallback=bool(args.allow_network_fallback),
            ): sym
            for sym in symbols
        }
        done = 0
        for ft in as_completed(futs):
            done += 1
            sym, df, err, elapsed = ft.result()
            if df is not None:
                all_frames.append(df)
                ok += 1
            else:
                fail += 1
                if len(fail_samples) < 10:
                    fail_samples.append(f"{sym}: {str(err)[:200]} (elapsed={elapsed:.1f}s)")
            if done % 500 == 0 or done == len(futs):
                print(f"[snapshot] {done}/{len(futs)} (ok={ok}, fail={fail})")

    if fail_samples:
        print("[snapshot] 失败样本:")
        for item in fail_samples[:10]:
            print(f"  - {item}")

    bench_main = None
    try:
        from integrations.data_source import _fetch_index_akshare

        bench_main = _fetch_index_akshare("000001", prefetch_start, end_s)
        print(f"[snapshot] 大盘指数 via akshare: {len(bench_main)} rows")
    except Exception as e1:
        print(f"[snapshot] akshare 大盘失败: {e1}, fallback fetch_index_hist")
        try:
            bench_main = fetch_index_hist("000001", prefetch_start, end_s)
        except Exception as e2:
            print(f"[snapshot] 大盘指数全部失败（不阻塞）: {e2}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not all_frames:
        print("[snapshot] 严重错误: 没有成功拉取任何股票数据!")
        return 1
    if ok < len(symbols) * 0.1:
        print(f"[snapshot] 严重错误: 成功率仅 {ok}/{len(symbols)} ({100 * ok / len(symbols):.1f}%)，低于 10% 阈值")
        return 1

    full_df = pd.concat(all_frames, ignore_index=True)
    full_df.to_csv(out_dir / "hist_full.csv.gz", index=False, compression="gzip")
    print(f"[snapshot] hist_full.csv.gz: {len(full_df)} rows")

    if bench_main is not None and not bench_main.empty:
        bench_main.to_csv(out_dir / "benchmark_main.csv", index=False)
        print(f"[snapshot] benchmark_main.csv: {len(bench_main)} rows")

    name_map: dict[str, str] = {}
    for item in raw_pool:
        if isinstance(item, dict):
            c = str(item.get("code", "")).strip()
            n = str(item.get("name", "")).strip()
            if c:
                name_map[c] = n
    (out_dir / "name_map.json").write_text(json.dumps(name_map, ensure_ascii=False), encoding="utf-8")
    print(f"[snapshot] name_map.json: {len(name_map)} entries")

    try:
        sm = fetch_sector_map()
        (out_dir / "sector_map.json").write_text(json.dumps(sm, ensure_ascii=False), encoding="utf-8")
        print(f"[snapshot] sector_map.json: {len(sm)} entries")
    except Exception as e:
        print(f"[snapshot] sector_map 拉取失败（不阻塞）: {e}")

    try:
        cm = fetch_market_cap_map()
        (out_dir / "market_cap_map.json").write_text(json.dumps(cm, ensure_ascii=False), encoding="utf-8")
        print(f"[snapshot] market_cap_map.json: {len(cm)} entries")
    except Exception as e:
        print(f"[snapshot] market_cap_map 拉取失败（不阻塞）: {e}")

    meta = {
        "symbols": len(symbols),
        "ok": ok,
        "fail": fail,
        "start": prefetch_start,
        "end": end_s,
        "allow_network_fallback": bool(args.allow_network_fallback),
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"[snapshot] Done! 成功率: {ok}/{len(symbols)} ({100 * ok / len(symbols):.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
