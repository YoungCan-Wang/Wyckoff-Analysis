# -*- coding: utf-8 -*-
"""
Wyckoff Funnel 定时任务：4 层漏斗筛选 → 飞书发送

Layer 1: 剥离垃圾 → Layer 2: 强弱甄别 → Layer 3: 板块共振 → Layer 4: 威科夫狙击
"""
from __future__ import annotations

import os
import socket
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from fetch_a_share_csv import (
    _resolve_trading_window,
    get_stocks_by_board,
    _normalize_symbols,
)
from wyckoff_engine import (
    FunnelConfig,
    layer1_filter,
    layer2_strength,
    layer3_sector_resonance,
    layer4_triggers,
    normalize_hist_from_fetch,
)
from data_source import fetch_index_hist, fetch_sector_map, fetch_market_cap_map
from utils.feishu import send_feishu_notification

TRIGGER_LABELS = {
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
}
TRADING_DAYS = 500
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2
SOCKET_TIMEOUT = 30
BATCH_TIMEOUT = 600
BATCH_SIZE = 200
BATCH_SLEEP = 5
MAX_WORKERS = 8
STEP3_MAX_SYMBOLS = 6


def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_hist_from_fetch(df)


def _fetch_hist(symbol: str, window, adjust: str) -> pd.DataFrame:
    from fetch_a_share_csv import _fetch_hist as _fh
    df = _fh(symbol=symbol, window=window, adjust=adjust)
    return _normalize_hist(df)


def _stock_name_map() -> dict[str, str]:
    try:
        from fetch_a_share_csv import get_all_stocks
        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}
    except Exception:
        return {}


def _fetch_one_with_retry(sym: str, window, max_retries: int = MAX_RETRIES) -> tuple[str, pd.DataFrame | None]:
    """在子进程中执行，进程级 socket 超时不影响主进程。"""
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    for attempt in range(max_retries):
        try:
            df = _fetch_hist(sym, window, "qfq")
            return (sym, df)
        except Exception:
            if attempt < max_retries - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
    return (sym, None)


def _terminate_executor_processes(ex: ProcessPoolExecutor, batch_no: int) -> None:
    """
    批次超时时，主动终止仍存活的子进程，避免 wait=False 仅“逻辑结束”但进程继续跑。
    这里使用私有属性是出于稳定性权衡：该任务更看重硬超时止损。
    """
    procs = getattr(ex, "_processes", {}) or {}
    killed = 0
    for proc in procs.values():
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=1)
                killed += 1
        except Exception as e:
            print(f"[funnel] 批次#{batch_no} 终止子进程异常: {e}")
    if killed:
        print(f"[funnel] 批次#{batch_no} 已强制终止 {killed} 个卡住子进程")


def run_funnel_job() -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    window = _resolve_trading_window(
        end_calendar_day=date.today() - timedelta(days=1),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    # 股票池：主板 + 创业板 - ST（预过滤，减少无效拉取）
    main_items = get_stocks_by_board("main")
    chinext_items = get_stocks_by_board("chinext")
    merged_code_to_name: dict[str, str] = {}
    for item in main_items + chinext_items:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        if code not in merged_code_to_name:
            merged_code_to_name[code] = str(item.get("name", "")).strip()
    merged_symbols = _normalize_symbols(list(merged_code_to_name.keys()))
    st_symbols = [
        sym for sym in merged_symbols
        if "ST" in merged_code_to_name.get(sym, "").upper()
    ]
    st_set = set(st_symbols)
    all_symbols = [sym for sym in merged_symbols if sym not in st_set]
    total_batches = (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    print(
        "[funnel] 股票池统计: "
        f"main={len(main_items)}, chinext={len(chinext_items)}, "
        f"merged={len(merged_symbols)}, st_excluded={len(st_symbols)}, "
        f"final={len(all_symbols)}, batches={total_batches} (batch_size={BATCH_SIZE})"
    )

    # 批量元数据
    print(f"[funnel] 加载行业映射...")
    sector_map = fetch_sector_map()
    print(f"[funnel] 加载市值数据...")
    market_cap_map = fetch_market_cap_map()
    if not market_cap_map:
        print("[funnel] ⚠️ 市值数据为空（TUSHARE_TOKEN 可能缺失/失效），Layer1 将跳过市值过滤")
    print(f"[funnel] 加载股票名称...")
    name_map = _stock_name_map()

    # 大盘基准
    bench_df = None
    try:
        bench_df = fetch_index_hist("000001", start_s, end_s)
        print(f"[funnel] 大盘基准加载成功")
    except Exception as e:
        print(f"[funnel] 大盘基准加载失败: {e}")

    # 并发拉取日线 + 分批漏斗（每批先做 L1/L2，最后统一做 L3/L4）
    l1_passed_set: set[str] = set()
    l2_passed_set: set[str] = set()
    l2_df_map: dict[str, pd.DataFrame] = {}
    fetch_ok = 0
    fetch_fail = 0

    print(
        f"[funnel] 开始拉取 {len(all_symbols)} 只股票日线 "
        f"(batch_size={BATCH_SIZE}, max_workers={MAX_WORKERS}, batch_timeout={BATCH_TIMEOUT}s)"
    )
    for i in range(0, len(all_symbols), BATCH_SIZE):
        batch_no = i // BATCH_SIZE + 1
        batch = all_symbols[i: i + BATCH_SIZE]
        batch_ok = 0
        batch_fail = 0
        batch_df_map: dict[str, pd.DataFrame] = {}
        batch_started = time.monotonic()
        print(f"[funnel] 批次#{batch_no}/{total_batches} 启动，股票数={len(batch)}")

        ex = ProcessPoolExecutor(max_workers=MAX_WORKERS)
        futures = {ex.submit(_fetch_one_with_retry, s, window): s for s in batch}
        try:
            for f in as_completed(futures, timeout=BATCH_TIMEOUT):
                sym = futures[f]
                try:
                    _, df = f.result()
                except Exception as e:
                    print(f"[funnel] 批次#{batch_no} 拉取失败 {sym}: {e}")
                    batch_fail += 1
                    fetch_fail += 1
                    continue
                if df is not None:
                    batch_ok += 1
                    fetch_ok += 1
                    batch_df_map[sym] = df
                else:
                    batch_fail += 1
                    fetch_fail += 1
        except TimeoutError:
            pending_symbols = [futures[ft] for ft in futures if not ft.done()]
            timed_out = len(pending_symbols)
            batch_fail += timed_out
            fetch_fail += timed_out
            print(
                f"[funnel] 批次#{batch_no} 超时({BATCH_TIMEOUT}s)，"
                f"已完成={batch_ok + batch_fail - timed_out}/{len(batch)}，"
                f"未完成={timed_out}，将跳过剩余任务"
            )
            if pending_symbols:
                preview = ", ".join(pending_symbols[:10])
                suffix = "..." if len(pending_symbols) > 10 else ""
                print(f"[funnel] 批次#{batch_no} 超时股票: {preview}{suffix}")
            _terminate_executor_processes(ex, batch_no)
        finally:
            for ft in futures:
                ft.cancel()
            ex.shutdown(wait=False, cancel_futures=True)

        batch_l1 = layer1_filter(batch, name_map, market_cap_map, batch_df_map, cfg)
        batch_l2 = layer2_strength(batch_l1, batch_df_map, bench_df, cfg)
        l1_passed_set.update(batch_l1)
        l2_passed_set.update(batch_l2)
        for sym in batch_l2:
            df = batch_df_map.get(sym)
            if df is not None:
                l2_df_map[sym] = df

        batch_elapsed = time.monotonic() - batch_started
        print(
            f"[funnel] 批次#{batch_no} 完成: 成功={batch_ok}, 失败={batch_fail}, "
            f"L1={len(batch_l1)}, L2={len(batch_l2)}, "
            f"耗时={batch_elapsed:.1f}s, 累计成功={fetch_ok}, 累计失败={fetch_fail}, "
            f"累计L1={len(l1_passed_set)}, 累计L2={len(l2_passed_set)}"
        )
        if i + BATCH_SIZE < len(all_symbols) and BATCH_SLEEP > 0:
            time.sleep(BATCH_SLEEP)

    print(f"[funnel] 日线拉取完成: 成功={fetch_ok}, 失败={fetch_fail}")

    # 汇总阶段：全局做 L3/L4（L3 依赖全局行业分布）
    l2_symbols = sorted(l2_passed_set)
    l3_symbols, top_sectors = layer3_sector_resonance(l2_symbols, sector_map, cfg)
    triggers = layer4_triggers(l3_symbols, l2_df_map, cfg)
    total_hits = sum(len(v) for v in triggers.values())
    metrics = {
        "total_symbols": len(all_symbols),
        "pool_main": len(main_items),
        "pool_chinext": len(chinext_items),
        "pool_merged": len(merged_symbols),
        "pool_st_excluded": len(st_symbols),
        "pool_batches": total_batches,
        "fetch_ok": fetch_ok,
        "fetch_fail": fetch_fail,
        "layer1": len(l1_passed_set),
        "layer2": len(l2_symbols),
        "layer3": len(l3_symbols),
        "top_sectors": top_sectors,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
    }
    print(f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
          f"L3={metrics['layer3']}, 命中={total_hits}, "
          f"Top行业={top_sectors}, 各触发={metrics['by_trigger']}")
    return triggers, metrics


def run(webhook_url: str) -> tuple[bool, list[dict]]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job()
    name_map = _stock_name_map()

    code_to_reasons: dict[str, list[str]] = {}
    code_to_best_score: dict[str, float] = {}
    for key, label in TRIGGER_LABELS.items():
        for code, score in triggers.get(key, []):
            if code not in code_to_reasons:
                code_to_reasons[code] = []
                code_to_best_score[code] = score
            code_to_reasons[code].append(label)
            code_to_best_score[code] = max(code_to_best_score.get(code, 0), score)

    sorted_codes = sorted(
        code_to_reasons.keys(),
        key=lambda c: -code_to_best_score.get(c, 0),
    )

    lines = [
        (
            f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
            f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
            f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
        ),
        f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 命中:{metrics['total_hits']}",
        f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        "",
        "**筛选结果（代码 名称 | 筛选理由）**",
        "",
    ]
    for code in sorted_codes:
        name = name_map.get(code, code)
        reasons = "、".join(code_to_reasons[code])
        lines.append(f"• {code} {name} | {reasons}")

    if not sorted_codes:
        lines.append("无")

    content = "\n".join(lines)
    title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
    ok = send_feishu_notification(webhook_url, title, content)

    symbols_for_report = [
        {
            "code": c,
            "name": name_map.get(c, c),
            "tag": "、".join(code_to_reasons[c]),
        }
        for c in sorted_codes[:STEP3_MAX_SYMBOLS]
    ]
    return (ok, symbols_for_report)
