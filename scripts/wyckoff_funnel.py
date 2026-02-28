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
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from integrations.fetch_a_share_csv import (
    _resolve_trading_window,
    get_stocks_by_board,
    _normalize_symbols,
)
from core.wyckoff_engine import (
    FunnelConfig,
    layer1_filter,
    layer2_strength,
    layer3_sector_resonance,
    layer4_triggers,
    normalize_hist_from_fetch,
)
from integrations.data_source import fetch_index_hist, fetch_sector_map, fetch_market_cap_map
from utils.feishu import send_feishu_notification

TRIGGER_LABELS = {
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
}
TRADING_DAYS = 500
MAX_RETRIES = int(os.getenv("FUNNEL_FETCH_RETRIES", "2"))
RETRY_BASE_DELAY = float(os.getenv("FUNNEL_RETRY_BASE_DELAY", "1.0"))
SOCKET_TIMEOUT = int(os.getenv("FUNNEL_SOCKET_TIMEOUT", "20"))
FETCH_TIMEOUT = int(os.getenv("FUNNEL_FETCH_TIMEOUT", "45"))
BATCH_TIMEOUT = int(os.getenv("FUNNEL_BATCH_TIMEOUT", "420"))
BATCH_SIZE = int(os.getenv("FUNNEL_BATCH_SIZE", "200"))
BATCH_SLEEP = float(os.getenv("FUNNEL_BATCH_SLEEP", "2"))
MAX_WORKERS = int(os.getenv("FUNNEL_MAX_WORKERS", "8"))
CN_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))


def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_hist_from_fetch(df)


def _fetch_hist(symbol: str, window, adjust: str) -> pd.DataFrame:
    from integrations.fetch_a_share_csv import _fetch_hist as _fh
    df = _fh(symbol=symbol, window=window, adjust=adjust)
    return _normalize_hist(df)


def _stock_name_map() -> dict[str, str]:
    try:
        from integrations.fetch_a_share_csv import get_all_stocks
        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}
    except Exception:
        return {}


def _fetch_one_with_retry(sym: str, window, max_retries: int = MAX_RETRIES) -> tuple[str, pd.DataFrame | None]:
    """在子进程中执行，单票硬超时 + 重试，避免个别数据源卡死拖慢整批。"""
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    for attempt in range(max_retries):
        try:
            df = _run_with_timeout(sym, window, FETCH_TIMEOUT)
            return (sym, df)
        except Exception:
            if attempt < max_retries - 1:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                time.sleep(delay)
    return (sym, None)


def _run_with_timeout(sym: str, window, timeout_s: int) -> pd.DataFrame:
    """
    在 worker 进程内给单票请求加硬超时（Unix 下用 SIGALRM）。
    若平台不支持 SIGALRM（例如 Windows），则退化为直接调用。
    注意：在 Windows / 不支持 SIGALRM 的运行环境里，本函数不会提供单票硬超时，
    仅依赖外层批次超时(BATCH_TIMEOUT)做兜底。
    """
    if timeout_s <= 0:
        return _fetch_hist(sym, window, "qfq")

    try:
        import signal
    except Exception:
        return _fetch_hist(sym, window, "qfq")

    if not hasattr(signal, "SIGALRM"):
        return _fetch_hist(sym, window, "qfq")

    def _alarm_handler(signum, frame):  # pragma: no cover - signal handler
        raise TimeoutError(f"single fetch timeout>{timeout_s}s")

    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout_s)
    try:
        return _fetch_hist(sym, window, "qfq")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _job_end_calendar_day() -> date:
    """
    定时任务统一口径：
    - 北京时间收盘后（默认 >=15:00）走 T+0（当天）
    - 收盘前走 T-1（上一自然日）
    """
    now = datetime.now(CN_TZ)
    if now.hour >= MARKET_CLOSE_HOUR:
        return now.date()
    return (now - timedelta(days=1)).date()


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


def _analyze_benchmark_and_tune_cfg(
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
) -> dict:
    """
    Step 0：大盘总闸
    - 输出宏观水温（RISK_ON / NEUTRAL / RISK_OFF）
    - 在 RISK_OFF 时动态收紧个股过滤阈值
    """
    context = {
        "regime": "UNKNOWN",
        "close": None,
        "ma50": None,
        "ma200": None,
        "ma50_slope_5d": None,
        "recent3_pct": [],
        "recent3_cum_pct": None,
        "tuned": {
            "min_avg_amount_wan": cfg.min_avg_amount_wan,
            "rs_min_long": cfg.rs_min_long,
            "rs_min_short": cfg.rs_min_short,
        },
    }
    if bench_df is None or bench_df.empty:
        return context

    b = bench_df.sort_values("date").copy()
    b["close"] = pd.to_numeric(b["close"], errors="coerce")
    b["pct_chg"] = pd.to_numeric(b["pct_chg"], errors="coerce")
    if len(b) < 60:
        return context

    close = float(b["close"].iloc[-1])
    ma50 = float(b["close"].rolling(50).mean().iloc[-1])
    ma200 = float(b["close"].rolling(200).mean().iloc[-1])
    ma50_prev = b["close"].rolling(50).mean().shift(5).iloc[-1]
    ma50_slope_5d = None if pd.isna(ma50_prev) else float(ma50 - ma50_prev)
    recent3 = b["pct_chg"].dropna().tail(3)
    recent3_list = [float(x) for x in recent3.tolist()]
    recent3_cum = None
    if not recent3.empty:
        recent3_cum = float(((recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0)

    regime = "NEUTRAL"
    if (
        pd.notna(ma200)
        and pd.notna(ma50)
        and ma50_slope_5d is not None
        and recent3_cum is not None
    ):
        risk_off = (close < ma200) and (ma50 < ma200) and (ma50_slope_5d < 0) and (recent3_cum <= -2.0)
        risk_on = (close > ma50 > ma200) and (ma50_slope_5d > 0) and (recent3_cum >= 0.0)
        if risk_off:
            regime = "RISK_OFF"
        elif risk_on:
            regime = "RISK_ON"

    # 动态调参：风险越冷，过滤越严
    if regime == "RISK_OFF":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 10000.0)
        cfg.rs_min_long = max(cfg.rs_min_long, 2.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.5)
        if recent3_cum is not None and recent3_cum <= -4.0:
            cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 15000.0)
            cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
            cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
    elif regime == "RISK_ON":
        cfg.rs_min_long = max(cfg.rs_min_long, 0.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.0)

    context.update({
        "regime": regime,
        "close": close,
        "ma50": ma50,
        "ma200": ma200,
        "ma50_slope_5d": ma50_slope_5d,
        "recent3_pct": recent3_list,
        "recent3_cum_pct": recent3_cum,
        "tuned": {
            "min_avg_amount_wan": cfg.min_avg_amount_wan,
            "rs_min_long": cfg.rs_min_long,
            "rs_min_short": cfg.rs_min_short,
        },
    })
    return context


def run_funnel_job() -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    window = _resolve_trading_window(
        end_calendar_day=_job_end_calendar_day(),
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

    # Step 0: 大盘总闸 + 动态阈值
    benchmark_context = _analyze_benchmark_and_tune_cfg(bench_df, cfg)
    print(
        "[funnel] 大盘总闸: "
        f"regime={benchmark_context['regime']}, "
        f"close={benchmark_context['close']}, ma50={benchmark_context['ma50']}, ma200={benchmark_context['ma200']}, "
        f"ma50_slope_5d={benchmark_context['ma50_slope_5d']}, recent3={benchmark_context['recent3_pct']}, "
        f"recent3_cum={benchmark_context['recent3_cum_pct']}, "
        f"tuned={benchmark_context['tuned']}"
    )

    # 并发拉取日线（只负责取数，不负责计算）
    all_df_map: dict[str, pd.DataFrame] = {}
    fetch_ok = 0
    fetch_fail = 0

    print(
        f"[funnel] 开始拉取 {len(all_symbols)} 只股票日线 "
        f"(batch_size={BATCH_SIZE}, max_workers={MAX_WORKERS}, batch_timeout={BATCH_TIMEOUT}s, "
        f"fetch_timeout={FETCH_TIMEOUT}s, retries={MAX_RETRIES})"
    )
    for i in range(0, len(all_symbols), BATCH_SIZE):
        batch_no = i // BATCH_SIZE + 1
        batch = all_symbols[i: i + BATCH_SIZE]
        batch_ok = 0
        batch_fail = 0
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
                    all_df_map[sym] = df
                else:
                    batch_fail += 1
                    fetch_fail += 1
        except FuturesTimeoutError:
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

        batch_elapsed = time.monotonic() - batch_started
        print(
            f"[funnel] 批次#{batch_no} 完成: 成功={batch_ok}, 失败={batch_fail}, "
            f"耗时={batch_elapsed:.1f}s, 累计成功={fetch_ok}, 累计失败={fetch_fail}"
        )
        if i + BATCH_SIZE < len(all_symbols) and BATCH_SLEEP > 0:
            time.sleep(BATCH_SLEEP)

    print(f"[funnel] 日线拉取完成: 成功={fetch_ok}, 失败={fetch_fail}")

    # 统一漏斗计算：L1 -> L2 -> L3 -> L4
    print(f"[funnel] 开始执行全量漏斗筛选...")
    
    # Layer 1
    l1_input = list(all_df_map.keys())
    l1_passed = layer1_filter(l1_input, name_map, market_cap_map, all_df_map, cfg)
    
    # Layer 2
    l2_passed = layer2_strength(l1_passed, all_df_map, bench_df, cfg)
    
    # Layer 3 (Sector Resonance)
    l3_passed, top_sectors = layer3_sector_resonance(l2_passed, sector_map, cfg)
    
    # Layer 4 (Wyckoff Triggers)
    # L4 需要 l2_df_map，这里直接用 all_df_map 即可，因为 key 都在里面
    triggers = layer4_triggers(l3_passed, all_df_map, cfg)
    
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
        "layer1": len(l1_passed),
        "layer2": len(l2_passed),
        "layer3": len(l3_passed),
        "top_sectors": top_sectors,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
        "benchmark_context": benchmark_context,
    }
    print(f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
          f"L3={metrics['layer3']}, 命中={total_hits}, "
          f"Top行业={top_sectors}, 各触发={metrics['by_trigger']}")
    return triggers, metrics


def run(webhook_url: str) -> tuple[bool, list[dict], dict]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表, 大盘上下文)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job()
    benchmark_context = metrics.get("benchmark_context", {}) or {}
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
    unique_hit_count = len(sorted_codes)
    selected_for_ai = sorted_codes

    print(
        f"[funnel] 候选分层: 命中事件={metrics['total_hits']}, 命中股票={unique_hit_count}, "
        f"AI输入=全量{len(selected_for_ai)}, "
        f"AI分析={len(selected_for_ai)}"
    )

    bench_line = "未知"
    if benchmark_context:
        bench_line = (
            f"{benchmark_context.get('regime')} | close={benchmark_context.get('close')} "
            f"ma50={benchmark_context.get('ma50')} ma200={benchmark_context.get('ma200')} "
            f"3d={benchmark_context.get('recent3_pct')} cum3={benchmark_context.get('recent3_cum_pct')}"
        )

    lines = [
        (
            f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
            f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
            f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
        ),
        f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 命中:{metrics['total_hits']}",
        f"**大盘水温**: {bench_line}",
        f"**候选分层**: 命中股票{unique_hit_count} -> AI输入全量{len(selected_for_ai)}",
        f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        "",
        "**命中列表（按优先级）代码 名称 | 筛选理由 | 分值**",
        "",
    ]
    for code in selected_for_ai:
        name = name_map.get(code, code)
        reasons = "、".join(code_to_reasons[code])
        lines.append(f"• {code} {name} | {reasons} | score={code_to_best_score.get(code, 0):.2f}")

    if not selected_for_ai:
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
        for c in selected_for_ai
    ]
    return (ok, symbols_for_report, benchmark_context)
