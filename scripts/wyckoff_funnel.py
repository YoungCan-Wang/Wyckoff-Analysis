# -*- coding: utf-8 -*-
# Copyright (c) 2024 youngcan. All Rights Reserved.
# 本代码仅供个人学习研究使用，未经授权不得用于商业目的。
# 商业授权请联系作者支付授权费用。

"""
Wyckoff Funnel 定时任务：4 层漏斗筛选 → 飞书发送

Layer 1: 剥离垃圾 → Layer 2: 强弱甄别 → Layer 3: 板块共振 → Layer 4: 威科夫狙击
"""

from __future__ import annotations
from dataclasses import fields as dataclass_fields
import json
import os
import socket
import sys
import time
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import date, datetime
from pathlib import Path

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
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
    normalize_hist_from_fetch,
    detect_markup_stage,
    detect_accum_stage,
    layer5_exit_signals,
    FunnelResult,
    allocate_ai_candidates,
    resolve_ai_candidate_policy,
)
from core.sector_rotation import (
    SECTOR_STATE_LABELS,
    SECTOR_STATE_SCORE_BONUS,
    analyze_sector_rotation,
)
from integrations.data_source import (
    fetch_index_hist,
    fetch_sector_map,
    fetch_market_cap_map,
    fetch_stock_spot_snapshot,
)
from utils.feishu import send_feishu_notification
from utils.trading_clock import CN_TZ, resolve_end_calendar_day

TRIGGER_LABELS = {
    "sos": "SOS（量价点火）",
    "spring": "Spring（终极震仓）",
    "lps": "LPS（缩量回踩）",
    "evr": "Effort vs Result（放量不跌）",
}
TRADING_DAYS = int(os.getenv("FUNNEL_TRADING_DAYS", "500"))
MAX_RETRIES = int(os.getenv("FUNNEL_FETCH_RETRIES", "2"))
RETRY_BASE_DELAY = float(os.getenv("FUNNEL_RETRY_BASE_DELAY", "1.0"))
SOCKET_TIMEOUT = int(os.getenv("FUNNEL_SOCKET_TIMEOUT", "20"))
FETCH_TIMEOUT = int(os.getenv("FUNNEL_FETCH_TIMEOUT", "45"))
BATCH_TIMEOUT = int(os.getenv("FUNNEL_BATCH_TIMEOUT", "420"))
BATCH_SIZE = int(os.getenv("FUNNEL_BATCH_SIZE", "250"))
BATCH_SLEEP = float(os.getenv("FUNNEL_BATCH_SLEEP", "2"))
MAX_WORKERS = int(os.getenv("FUNNEL_MAX_WORKERS", "8"))
EXECUTOR_MODE = os.getenv("FUNNEL_EXECUTOR_MODE", "process").strip().lower()
if EXECUTOR_MODE not in {"thread", "process"}:
    EXECUTOR_MODE = "process"
ENFORCE_TARGET_TRADE_DATE = False
FUNNEL_ENABLE_SPOT_PATCH = os.getenv("FUNNEL_ENABLE_SPOT_PATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_SPOT_PATCH_RETRIES = int(os.getenv("FUNNEL_SPOT_PATCH_RETRIES", "2"))
FUNNEL_SPOT_PATCH_SLEEP = float(os.getenv("FUNNEL_SPOT_PATCH_SLEEP", "0.2"))
BREADTH_MA_WINDOW = int(os.getenv("FUNNEL_BREADTH_MA_WINDOW", "20"))
BREADTH_RISK_OFF_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_OFF_PCT", "20.0"))
BREADTH_RISK_ON_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_ON_PCT", "60.0"))
BREADTH_RISK_ON_MIN_DELTA = float(os.getenv("FUNNEL_BREADTH_RISK_ON_DELTA", "0.0"))
BREADTH_CLIFF_DROP_PCT = float(os.getenv("FUNNEL_BREADTH_CLIFF_DROP_PCT", "-10.0"))
SMALLCAP_BENCH_CODE = os.getenv("FUNNEL_SMALLCAP_BENCH_CODE", "399006").strip() or "399006"
CRASH_MAIN_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_MAIN_DAY_DROP_PCT", "-1.3"))
CRASH_SMALL_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_SMALL_DAY_DROP_PCT", "-2.5"))
CRASH_BREADTH_RATIO_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_RATIO_PCT", "15.0"))
CRASH_BREADTH_DELTA_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_DELTA_PCT", "-20.0"))
PANIC_REPAIR_MIN_AVG_AMOUNT_WAN = float(
    os.getenv("FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN", "7000.0")
)
RISK_OFF_MIN_AVG_AMOUNT_WAN = float(
    os.getenv("FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN", "8000.0")
)
RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN = float(
    os.getenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "10000.0")
)
CRASH_MIN_AVG_AMOUNT_WAN = float(
    os.getenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "12000.0")
)
PANIC_REPAIR_ENABLE = os.getenv("FUNNEL_PANIC_REPAIR_ENABLE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PANIC_REPAIR_MAIN_REBOUND_PCT = float(
    os.getenv("FUNNEL_PANIC_REPAIR_MAIN_REBOUND_PCT", "0.8")
)
PANIC_REPAIR_SMALL_REBOUND_PCT = float(
    os.getenv("FUNNEL_PANIC_REPAIR_SMALL_REBOUND_PCT", "1.5")
)
FUNNEL_EXPORT_FULL_FETCH = os.getenv("FUNNEL_EXPORT_FULL_FETCH", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_EXPORT_DIR = os.getenv("FUNNEL_EXPORT_DIR", "data/funnel_snapshots").strip() or "data/funnel_snapshots"
FUNNEL_AI_SELECTION_MODE = (
    os.getenv("FUNNEL_AI_SELECTION_MODE", "legacy_full_hits").strip().lower()
)
FUNNEL_CARD_STYLE = os.getenv("FUNNEL_CARD_STYLE", "legacy_compact").strip().lower()


def _parse_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except Exception:
        return default


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _apply_funnel_cfg_overrides(cfg: FunnelConfig) -> None:
    """
    将 .env 中的 FUNNEL_CFG_* 参数映射到 FunnelConfig。
    示例：FUNNEL_CFG_MIN_MARKET_CAP_YI=35
    """
    for f in dataclass_fields(FunnelConfig):
        if f.name == "enable_evr_trigger":
            # EVR 仅由 regime 自动决策，不接受环境变量覆盖。
            continue
        key = f"FUNNEL_CFG_{f.name.upper()}"
        raw = os.getenv(key)
        if raw is None:
            continue
        val = raw.strip()
        if not val:
            continue
        try:
            current = getattr(cfg, f.name, None)
            if isinstance(current, bool):
                parsed = _parse_bool(val)
            elif isinstance(current, int) and not isinstance(current, bool):
                parsed = int(float(val))
            elif isinstance(current, float):
                parsed = float(val)
            else:
                parsed = val
            setattr(cfg, f.name, parsed)
        except Exception as e:
            print(f"[funnel] ⚠️ 忽略非法配置 {key}={raw!r}: {e}")


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
        return {
            x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)
        }
    except Exception:
        return {}


def _fetch_one_with_retry(
    sym: str, window, max_retries: int = MAX_RETRIES
) -> tuple[str, pd.DataFrame | None]:
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


def _fetch_one_with_retry_thread(
    sym: str, window, max_retries: int = MAX_RETRIES
) -> tuple[str, pd.DataFrame | None]:
    """
    线程模式：避免 signal，依赖数据源请求超时与重试。
    """
    for attempt in range(max_retries):
        try:
            df = _fetch_hist(sym, window, "qfq")
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
    - 北京时间 17:00-23:59 走 T（当天）
    - 北京时间 00:00-16:59 走 T-1（上一自然日）
    """
    return resolve_end_calendar_day()


def _resolve_symbol_pool_from_env() -> tuple[list[str], dict[str, str], dict[str, int | str]]:
    pool_mode = str(os.getenv("FUNNEL_POOL_MODE", "") or "").strip().lower()
    limit_count = max(_parse_int_env("FUNNEL_POOL_LIMIT_COUNT", 0), 0)

    if pool_mode == "manual":
        manual_raw = str(os.getenv("FUNNEL_POOL_MANUAL_SYMBOLS", "") or "")
        all_name_map = _stock_name_map()
        symbols = _normalize_symbols(
            [x.strip() for x in manual_raw.replace(";", ",").replace("\n", ",").split(",")]
        )
        name_map = {code: all_name_map.get(code, "") for code in symbols}
        return (
            symbols,
            name_map,
            {
                "pool_mode": "manual",
                "pool_main": 0,
                "pool_chinext": 0,
                "pool_merged": len(symbols),
                "pool_st_excluded": 0,
                "pool_limit": limit_count,
            },
        )

    board_name = str(os.getenv("FUNNEL_POOL_BOARD", "") or "").strip().lower()
    if pool_mode == "board" and board_name in {"main", "chinext", "all"}:
        if board_name == "all":
            items = get_stocks_by_board("main") + get_stocks_by_board("chinext")
        else:
            items = get_stocks_by_board(board_name)
        merged_code_to_name: dict[str, str] = {}
        for item in items:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            if code not in merged_code_to_name:
                merged_code_to_name[code] = str(item.get("name", "")).strip()
        symbols = _normalize_symbols(list(merged_code_to_name.keys()))
        if limit_count > 0:
            symbols = symbols[:limit_count]
        return (
            symbols,
            {code: merged_code_to_name.get(code, "") for code in symbols},
            {
                "pool_mode": "board",
                "pool_main": len(items) if board_name == "main" else len(get_stocks_by_board("main")) if board_name == "all" else 0,
                "pool_chinext": len(items) if board_name == "chinext" else len(get_stocks_by_board("chinext")) if board_name == "all" else 0,
                "pool_merged": len(symbols),
                "pool_st_excluded": 0,
                "pool_limit": limit_count,
            },
        )

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
        sym for sym in merged_symbols if "ST" in merged_code_to_name.get(sym, "").upper()
    ]
    st_set = set(st_symbols)
    all_symbols = [sym for sym in merged_symbols if sym not in st_set]
    if limit_count > 0:
        all_symbols = all_symbols[:limit_count]
    return (
        all_symbols,
        {code: merged_code_to_name.get(code, "") for code in all_symbols},
        {
            "pool_mode": "default",
            "pool_main": len(main_items),
            "pool_chinext": len(chinext_items),
            "pool_merged": len(merged_symbols),
            "pool_st_excluded": len(st_symbols),
            "pool_limit": limit_count,
        },
    )


def _latest_trade_date_from_hist(df: pd.DataFrame) -> date | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if s.empty:
        return None
    return s.iloc[-1].date()


def _append_spot_bar_if_needed(
    symbol: str,
    df: pd.DataFrame,
    target_trade_date: date,
) -> tuple[pd.DataFrame, bool]:
    if not FUNNEL_ENABLE_SPOT_PATCH or df is None or df.empty:
        return (df, False)
    latest_trade_date = _latest_trade_date_from_hist(df)
    if latest_trade_date is None or latest_trade_date >= target_trade_date:
        return (df, False)
    if target_trade_date != datetime.now(CN_TZ).date():
        return (df, False)

    df_s = df.sort_values("date").reset_index(drop=True)
    last_close_series = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    prev_close = float(last_close_series.iloc[-1]) if not last_close_series.empty else None
    prev_volume = None
    prev_amount = None
    if "volume" in df_s.columns:
        vol_s = pd.to_numeric(df_s.get("volume"), errors="coerce").dropna()
        if not vol_s.empty:
            prev_volume = float(vol_s.iloc[-1])
    if "amount" in df_s.columns:
        amt_s = pd.to_numeric(df_s.get("amount"), errors="coerce").dropna()
        if not amt_s.empty:
            prev_amount = float(amt_s.iloc[-1])

    for attempt in range(max(FUNNEL_SPOT_PATCH_RETRIES, 1)):
        snap = fetch_stock_spot_snapshot(symbol, force_refresh=attempt > 0)
        close_v = None if not snap else snap.get("close")
        if close_v is None or float(close_v) <= 0:
            if attempt < max(FUNNEL_SPOT_PATCH_RETRIES, 1) - 1:
                time.sleep(max(FUNNEL_SPOT_PATCH_SLEEP, 0.0))
            continue

        close_f = float(close_v)
        open_f = float(snap.get("open")) if snap and snap.get("open") is not None else close_f
        high_raw = float(snap.get("high")) if snap and snap.get("high") is not None else close_f
        low_raw = float(snap.get("low")) if snap and snap.get("low") is not None else close_f
        high_f = max(high_raw, open_f, close_f)
        low_f = min(low_raw, open_f, close_f)
        turnover_ok = bool(float(snap.get("turnover_unit_ok", 0.0))) if snap else False
        if turnover_ok:
            volume_f = float(snap.get("volume")) if snap.get("volume") is not None else 0.0
            amount_f = float(snap.get("amount")) if snap.get("amount") is not None else 0.0
        else:
            # 单位不可信时仅补价格，量额沿用上一交易日，避免把量能信号污染为 0。
            volume_f = float(prev_volume) if prev_volume is not None else float("nan")
            amount_f = float(prev_amount) if prev_amount is not None else float("nan")
        pct_f = float(snap.get("pct_chg")) if snap and snap.get("pct_chg") is not None else None
        if pct_f is None and prev_close and prev_close > 0:
            pct_f = (close_f - prev_close) / prev_close * 100.0

        new_row = {
            "date": target_trade_date.isoformat(),
            "open": open_f,
            "high": high_f,
            "low": low_f,
            "close": close_f,
            "volume": volume_f,
            "amount": amount_f,
            "pct_chg": pct_f if pct_f is not None else 0.0,
        }
        patched = pd.concat([df_s, pd.DataFrame([new_row])], ignore_index=True)
        patched = patched.sort_values("date").reset_index(drop=True)
        return (patched, True)
    return (df, False)


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
    smallcap_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    breadth: dict | None = None,
) -> dict:
    """
    Step 0：大盘总闸
    - 输出宏观水温（RISK_ON / NEUTRAL / RISK_OFF）
    - 在 RISK_OFF 时动态收紧个股过滤阈值
    """
    context = {
        "regime": "UNKNOWN",
        "main_code": "000001",
        "close": None,
        "ma50": None,
        "ma200": None,
        "ma50_slope_5d": None,
        "recent3_pct": [],
        "recent3_cum_pct": None,
        "smallcap_code": SMALLCAP_BENCH_CODE,
        "smallcap_close": None,
        "smallcap_recent3_pct": [],
        "smallcap_recent3_cum_pct": None,
        "smallcap_today_pct": None,
        "panic_triggered": False,
        "panic_reasons": [],
        "repair_triggered": False,
        "repair_reasons": [],
        "tuned": {
            "min_avg_amount_wan": cfg.min_avg_amount_wan,
            "rs_min_long": cfg.rs_min_long,
            "rs_min_short": cfg.rs_min_short,
            "rps_fast_min": cfg.rps_fast_min,
            "rps_slow_min": cfg.rps_slow_min,
        },
        "breadth": {
            "ratio_pct": None,
            "prev_ratio_pct": None,
            "delta_pct": None,
            "sample_size": 0,
            "ma_window": BREADTH_MA_WINDOW,
        },
    }
    close = None
    ma50 = None
    ma200 = None
    ma50_slope_5d = None
    recent3_list: list[float] = []
    recent3_cum = None
    main_today_pct = None
    main_prev_pct = None
    small_close = None
    small_recent3_list: list[float] = []
    small_recent3_cum = None
    small_today_pct = None
    small_prev_pct = None

    if bench_df is not None and not bench_df.empty:
        b = bench_df.sort_values("date").copy()
        b["close"] = pd.to_numeric(b["close"], errors="coerce")
        b["pct_chg"] = pd.to_numeric(b["pct_chg"], errors="coerce")
        if len(b) >= 60:
            close = float(b["close"].iloc[-1])
            ma50 = float(b["close"].rolling(50).mean().iloc[-1])
            ma200 = float(b["close"].rolling(200).mean().iloc[-1])
            ma50_prev = b["close"].rolling(50).mean().shift(5).iloc[-1]
            ma50_slope_5d = None if pd.isna(ma50_prev) else float(ma50 - ma50_prev)
            recent3 = b["pct_chg"].dropna().tail(3)
            recent3_list = [float(x) for x in recent3.tolist()]
            if not recent3.empty:
                recent3_cum = float(((recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0)
            if recent3_list:
                main_today_pct = float(recent3_list[-1])
                if len(recent3_list) >= 2:
                    main_prev_pct = float(recent3_list[-2])

    if smallcap_df is not None and not smallcap_df.empty:
        s = smallcap_df.sort_values("date").copy()
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        s["pct_chg"] = pd.to_numeric(s["pct_chg"], errors="coerce")
        if len(s) >= 10:
            small_close = float(s["close"].iloc[-1])
            s_recent3 = s["pct_chg"].dropna().tail(3)
            small_recent3_list = [float(x) for x in s_recent3.tolist()]
            if not s_recent3.empty:
                small_recent3_cum = float(
                    ((s_recent3 / 100.0 + 1.0).prod() - 1.0) * 100.0
                )
            if small_recent3_list:
                small_today_pct = float(small_recent3_list[-1])
                if len(small_recent3_list) >= 2:
                    small_prev_pct = float(small_recent3_list[-2])

    regime = "NEUTRAL"
    if (
        ma200 is not None
        and ma50 is not None
        and ma50_slope_5d is not None
        and recent3_cum is not None
        and close is not None
    ):
        risk_off = (
            (close < ma200)
            and (ma50 < ma200)
            and (ma50_slope_5d < 0)
            and (recent3_cum <= -2.0)
        )
        risk_on = (
            (close > ma50 > ma200) and (ma50_slope_5d > 0) and (recent3_cum >= 0.0)
        )
        if risk_off:
            regime = "RISK_OFF"
        elif risk_on:
            regime = "RISK_ON"

    breadth_ratio = None
    breadth_prev = None
    breadth_delta = None
    breadth_sample = 0
    if breadth:
        breadth_ratio = breadth.get("ratio_pct")
        breadth_prev = breadth.get("prev_ratio_pct")
        breadth_delta = breadth.get("delta_pct")
        breadth_sample = int(breadth.get("sample_size") or 0)
    if breadth_ratio is not None:
        if float(breadth_ratio) <= BREADTH_RISK_OFF_THRESHOLD:
            regime = "RISK_OFF"
        elif float(breadth_ratio) >= BREADTH_RISK_ON_THRESHOLD:
            if breadth_delta is None or float(breadth_delta) >= BREADTH_RISK_ON_MIN_DELTA:
                regime = "RISK_ON"

        # 强力悬崖检测 (Breadth Cliff Drop): 赚了指数不赚钱，暗流涌动的隐性雪崩
        if breadth_delta is not None and float(breadth_delta) <= BREADTH_CLIFF_DROP_PCT:
            regime = "RISK_OFF"

    panic_reasons: list[str] = []
    if main_today_pct is not None and float(main_today_pct) <= float(CRASH_MAIN_DAY_DROP_PCT):
        panic_reasons.append(
            f"main_day_drop={main_today_pct:.2f}%<=阈值{CRASH_MAIN_DAY_DROP_PCT:.2f}%"
        )
    if small_today_pct is not None and float(small_today_pct) <= float(CRASH_SMALL_DAY_DROP_PCT):
        panic_reasons.append(
            f"smallcap_day_drop={small_today_pct:.2f}%<=阈值{CRASH_SMALL_DAY_DROP_PCT:.2f}%"
        )
    if breadth_ratio is not None and float(breadth_ratio) <= float(CRASH_BREADTH_RATIO_PCT):
        panic_reasons.append(
            f"breadth_ratio={float(breadth_ratio):.2f}%<=阈值{CRASH_BREADTH_RATIO_PCT:.2f}%"
        )
    if breadth_delta is not None and float(breadth_delta) <= float(CRASH_BREADTH_DELTA_PCT):
        panic_reasons.append(
            f"breadth_delta={float(breadth_delta):.2f}%<=阈值{CRASH_BREADTH_DELTA_PCT:.2f}%"
        )
    repair_reasons: list[str] = []
    if panic_reasons:
        regime = "CRASH"
    elif PANIC_REPAIR_ENABLE:
        # 改进逻辑：支持连续反弹（前 1-2 天是 CRASH，最近 1-2 天反弹）
        prev_panic = (
            (main_prev_pct is not None and float(main_prev_pct) <= float(CRASH_MAIN_DAY_DROP_PCT))
            or (
                small_prev_pct is not None
                and float(small_prev_pct) <= float(CRASH_SMALL_DAY_DROP_PCT)
            )
        )
        rebound_ok = (
            (main_today_pct is not None and float(main_today_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT))
            or (
                small_today_pct is not None
                and float(small_today_pct) >= float(PANIC_REPAIR_SMALL_REBOUND_PCT)
            )
        )
        # 连续反弹：最近 2 日都反弹
        continuous_rebound = False
        if main_today_pct is not None and main_prev_pct is not None:
            continuous_rebound = (
                float(main_today_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT) * 0.5
                and float(main_prev_pct) >= float(PANIC_REPAIR_MAIN_REBOUND_PCT) * 0.5
            )
        
        if (prev_panic and rebound_ok) or continuous_rebound:
            regime = "PANIC_REPAIR"
            repair_reasons = [
                f"prev_panic(main_prev={main_prev_pct}, small_prev={small_prev_pct})",
                f"rebound_ok(main_today={main_today_pct}, small_today={small_today_pct})",
            ]

    # EVR 按市场水温自动开关（不受环境变量覆盖）。
    # 冷市场（RISK_OFF/CRASH）开启 EVR 防派发；其余关闭以减少踏空。
    cfg.enable_evr_trigger = regime in {"RISK_OFF", "CRASH"}

    # 动态调参：风险越冷，过滤越严
    if regime == "CRASH":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, CRASH_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)  # 改为 80.0（从 90.0）
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)  # 改为 75.0（从 85.0）
    elif regime == "PANIC_REPAIR":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, PANIC_REPAIR_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 1.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.2)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 75.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 65.0)
    elif regime == "RISK_OFF":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, RISK_OFF_MIN_AVG_AMOUNT_WAN)
        cfg.rs_min_long = max(cfg.rs_min_long, 2.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.5)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
        if recent3_cum is not None and recent3_cum <= -4.0:
            cfg.min_avg_amount_wan = max(
                cfg.min_avg_amount_wan,
                RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN,
            )
            cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
            cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
    elif regime == "RISK_ON":
        cfg.rs_min_long = max(cfg.rs_min_long, 0.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.0)
        cfg.rps_fast_min = min(cfg.rps_fast_min, 70.0)
        cfg.rps_slow_min = min(cfg.rps_slow_min, 60.0)

    context.update(
        {
            "regime": regime,
            "close": close,
            "ma50": ma50,
            "ma200": ma200,
            "ma50_slope_5d": ma50_slope_5d,
            "recent3_pct": recent3_list,
            "recent3_cum_pct": recent3_cum,
            "main_today_pct": main_today_pct,
            "smallcap_close": small_close,
            "smallcap_recent3_pct": small_recent3_list,
            "smallcap_recent3_cum_pct": small_recent3_cum,
            "smallcap_today_pct": small_today_pct,
            "panic_triggered": bool(panic_reasons),
            "panic_reasons": panic_reasons,
            "repair_triggered": bool(repair_reasons),
            "repair_reasons": repair_reasons,
            "tuned": {
                "min_avg_amount_wan": cfg.min_avg_amount_wan,
                "rs_min_long": cfg.rs_min_long,
                "rs_min_short": cfg.rs_min_short,
                "rps_fast_min": cfg.rps_fast_min,
                "rps_slow_min": cfg.rps_slow_min,
                "enable_evr_trigger": bool(cfg.enable_evr_trigger),
            },
            "breadth": {
                "ratio_pct": breadth_ratio,
                "prev_ratio_pct": breadth_prev,
                "delta_pct": breadth_delta,
                "sample_size": breadth_sample,
                "ma_window": BREADTH_MA_WINDOW,
            },
        }
    )
    return context


def _calc_market_breadth(
    df_map: dict[str, pd.DataFrame],
    ma_window: int = BREADTH_MA_WINDOW,
) -> dict:
    """
    全市场广度：
    breadth = 收盘价站上 MA20 的股票占比（%）。
    额外给出前一日广度与日变化，用于识别扩散/收敛。
    """
    valid_now = 0
    valid_prev = 0
    above_now = 0
    above_prev = 0
    w = max(int(ma_window), 2)
    for df in df_map.values():
        if df is None or df.empty:
            continue
        s = df
        if "date" in s.columns:
            try:
                if not s["date"].is_monotonic_increasing:
                    s = s.sort_values("date")
            except Exception:
                s = s.sort_values("date")

        close = pd.to_numeric(s.get("close"), errors="coerce").dropna().tail(w + 1)
        if len(close) < (w + 1):
            continue

        c_now = float(close.iloc[-1])
        ma_now = float(close.iloc[1:].mean())
        c_prev = float(close.iloc[-2])
        ma_prev = float(close.iloc[:-1].mean())

        valid_now += 1
        if c_now >= ma_now:
            above_now += 1

        valid_prev += 1
        if c_prev >= ma_prev:
            above_prev += 1

    ratio_now = (above_now / valid_now * 100.0) if valid_now > 0 else None
    ratio_prev = (above_prev / valid_prev * 100.0) if valid_prev > 0 else None
    delta = None
    if ratio_now is not None and ratio_prev is not None:
        delta = ratio_now - ratio_prev
    return {
        "ratio_pct": ratio_now,
        "prev_ratio_pct": ratio_prev,
        "delta_pct": delta,
        "sample_size": valid_now,
    }


def _dump_full_fetch_snapshot(
    df_map: dict[str, pd.DataFrame],
    all_symbols: list[str],
    window,
    fetch_stats: dict,
    bench_df: pd.DataFrame | None = None,
    smallcap_df: pd.DataFrame | None = None,
) -> str | None:
    """
    将本轮全量拉取结果落盘，便于后续离线复现和自测。
    导出内容：
    - hist_full.csv.gz: 全量历史（日线）明细（含 symbol 列）
    - latest_quotes.csv: 每只股票最新一条记录
    - fetch_status.csv: 每只股票拉取状态
    - benchmark_main.csv / benchmark_smallcap.csv: 基准指数日线
    - metadata.json: 运行元信息
    """
    if not FUNNEL_EXPORT_FULL_FETCH:
        return None
    if not all_symbols:
        return None

    try:
        base_dir = Path(FUNNEL_EXPORT_DIR)
        base_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
        run_dir = base_dir / f"full_fetch_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

        frames: list[pd.DataFrame] = []
        status_rows: list[dict] = []
        for symbol in all_symbols:
            df = df_map.get(symbol)
            if df is None or df.empty:
                status_rows.append(
                    {
                        "symbol": symbol,
                        "fetched": 0,
                        "rows": 0,
                        "latest_trade_date": "",
                    }
                )
                continue

            one = df.copy()
            one.insert(0, "symbol", symbol)
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            frames.append(one)
            latest_trade_date = _latest_trade_date_from_hist(df)
            status_rows.append(
                {
                    "symbol": symbol,
                    "fetched": 1,
                    "rows": int(len(df)),
                    "latest_trade_date": latest_trade_date.isoformat() if latest_trade_date else "",
                }
            )

        full_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        full_path = run_dir / "hist_full.csv.gz"
        full_df.to_csv(full_path, index=False, compression="gzip")

        if not full_df.empty and {"symbol", "date"}.issubset(full_df.columns):
            latest_df = (
                full_df.sort_values(["symbol", "date"])
                .groupby("symbol", as_index=False)
                .tail(1)
                .sort_values("symbol")
                .reset_index(drop=True)
            )
        else:
            latest_df = pd.DataFrame(columns=["symbol"])
        latest_df.to_csv(run_dir / "latest_quotes.csv", index=False)

        status_df = pd.DataFrame(status_rows).sort_values("symbol").reset_index(drop=True)
        status_df.to_csv(run_dir / "fetch_status.csv", index=False)

        def _dump_benchmark(df_src: pd.DataFrame | None, filename: str) -> bool:
            if df_src is None or df_src.empty:
                return False
            cols = [c for c in ["date", "open", "high", "low", "close", "volume", "pct_chg"] if c in df_src.columns]
            if not cols:
                return False
            one = df_src[cols].copy()
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            one.to_csv(run_dir / filename, index=False)
            return True

        has_bench_main = _dump_benchmark(bench_df, "benchmark_main.csv")
        has_bench_smallcap = _dump_benchmark(smallcap_df, "benchmark_smallcap.csv")

        metadata = {
            "generated_at": datetime.now(CN_TZ).isoformat(),
            "export_dir": str(run_dir),
            "window_start_trade_date": window.start_trade_date.isoformat(),
            "window_end_trade_date": window.end_trade_date.isoformat(),
            "symbols_total": int(len(all_symbols)),
            "symbols_fetched": int(sum(1 for s in status_rows if s["fetched"] == 1)),
            "rows_total": int(len(full_df)),
            "fetch_stats": fetch_stats,
            "has_benchmark_main": has_bench_main,
            "has_benchmark_smallcap": has_bench_smallcap,
        }
        with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        with open(base_dir / "latest_run.txt", "w", encoding="utf-8") as f:
            f.write(str(run_dir) + "\n")

        print(
            "[funnel] 全量快照已落盘: "
            f"{run_dir} (symbols={metadata['symbols_fetched']}/{metadata['symbols_total']}, "
            f"rows={metadata['rows_total']})"
        )
        return str(run_dir)
    except Exception as e:
        print(f"[funnel] ⚠️ 全量快照落盘失败: {e}")
        return None


def _calc_close_return_pct(close_series: pd.Series, lookback: int) -> float | None:
    s = pd.to_numeric(close_series, errors="coerce").dropna()
    lb = max(int(lookback), 1)
    if len(s) <= lb:
        return None
    start = float(s.iloc[-lb - 1])
    end = float(s.iloc[-1])
    if start <= 0:
        return None
    return (end - start) / start * 100.0


def _rank_l3_candidates(
    l3_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    top_sectors: list[str],
    l2_channel_map: dict[str, str] | None = None,
    sector_rotation_map: dict[str, dict] | None = None,
) -> tuple[list[str], dict[str, float]]:
    """
    对 L3 股票做统一优先级排序，仅用于 AI 输入队列。
    """
    if not l3_symbols:
        return ([], {})

    trigger_score_map: dict[str, float] = {}
    for key in TRIGGER_LABELS.keys():
        for code, score in triggers.get(key, []):
            trigger_score_map[code] = max(trigger_score_map.get(code, 0.0), float(score))

    rows: list[dict] = []
    channel_map = l2_channel_map or {}
    rotation_map = sector_rotation_map or {}
    for code in l3_symbols:
        df = df_map.get(code)
        industry = str(sector_map.get(code, "") or "未知行业")
        l2_channel = str(channel_map.get(code, "") or "未标注通道")
        sector_state = str((rotation_map.get(industry, {}) or {}).get("state", "") or "")
        ret20 = None
        ret5 = None
        min_vol_ratio_5d = None
        if df is not None and not df.empty:
            s = df.sort_values("date")
            close = pd.to_numeric(s.get("close"), errors="coerce")
            volume = pd.to_numeric(s.get("volume"), errors="coerce")
            ret20 = _calc_close_return_pct(close, 20)
            ret5 = _calc_close_return_pct(close, 5)
            vol_ma20 = volume.rolling(20).mean()
            vol_ratio = volume / vol_ma20.replace(0, pd.NA)
            min_vol_ratio_5d = pd.to_numeric(vol_ratio.tail(5), errors="coerce").min()

        rows.append(
            {
                "code": code,
                "industry": industry,
                "ret20": ret20,
                "ret5": ret5,
                "min_vol_ratio_5d": min_vol_ratio_5d,
                "trigger_score": float(trigger_score_map.get(code, 0.0)),
                "l2_channel": l2_channel,
                "sector_state": sector_state,
            }
        )

    rank_df = pd.DataFrame(rows)
    for col, fill_default in (("ret20", 0.0), ("ret5", 0.0), ("min_vol_ratio_5d", 1.0)):
        rank_df[col] = pd.to_numeric(rank_df[col], errors="coerce")
        if rank_df[col].notna().any():
            rank_df[col] = rank_df[col].fillna(float(rank_df[col].median()))
        else:
            rank_df[col] = rank_df[col].fillna(fill_default)

    rank_df["q20"] = rank_df["ret20"].rank(pct=True, ascending=True, method="average")
    rank_df["q5"] = rank_df["ret5"].rank(pct=True, ascending=True, method="average")
    rank_df["dry_q"] = rank_df["min_vol_ratio_5d"].rank(
        pct=True, ascending=False, method="average"
    )
    if rank_df["trigger_score"].nunique(dropna=False) > 1:
        rank_df["trigger_q"] = rank_df["trigger_score"].rank(
            pct=True, ascending=True, method="average"
        )
    else:
        rank_df["trigger_q"] = rank_df["trigger_score"].apply(
            lambda x: 1.0 if float(x) > 0 else 0.0
        )

    hot_sector_set = set(top_sectors or [])
    rank_df["hot_bonus"] = rank_df["industry"].isin(hot_sector_set).astype(float) * 0.05
    rank_df["sector_bonus"] = rank_df["sector_state"].map(
        lambda x: float(SECTOR_STATE_SCORE_BONUS.get(str(x), 0.0))
    )
    rank_df["watch_score"] = (
        0.40 * rank_df["q20"]
        + 0.25 * rank_df["q5"]
        + 0.20 * rank_df["dry_q"]
        + 0.15 * rank_df["trigger_q"]
        + rank_df["hot_bonus"]
        + rank_df["sector_bonus"]
    )

    rank_df = rank_df.sort_values("watch_score", ascending=False).reset_index(drop=True)
    ranked_symbols = rank_df["code"].astype(str).tolist()
    score_map = {
        str(r["code"]): float(r["watch_score"])
        for _, r in rank_df.iterrows()
    }
    return (ranked_symbols, score_map)


def run_funnel_job(
    include_debug_context: bool = False,
) -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    _apply_funnel_cfg_overrides(cfg)
    window = _resolve_trading_window(
        end_calendar_day=_job_end_calendar_day(),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    all_symbols, pool_name_map, pool_stats = _resolve_symbol_pool_from_env()
    main_items = [None] * int(pool_stats.get("pool_main", 0) or 0)
    chinext_items = [None] * int(pool_stats.get("pool_chinext", 0) or 0)
    merged_symbols = list(pool_name_map.keys())
    st_symbols = [None] * int(pool_stats.get("pool_st_excluded", 0) or 0)
    total_batches = (
        (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    )
    print(
        "[funnel] 股票池统计: "
        f"mode={pool_stats.get('pool_mode')}, main={len(main_items)}, chinext={len(chinext_items)}, "
        f"merged={len(merged_symbols)}, st_excluded={len(st_symbols)}, "
        f"final={len(all_symbols)}, limit={pool_stats.get('pool_limit', 0)}, batches={total_batches} (batch_size={BATCH_SIZE})"
    )

    # 批量元数据
    print(f"[funnel] 加载行业映射...")
    sector_map = fetch_sector_map()
    print(f"[funnel] 加载市值数据...")
    market_cap_map = fetch_market_cap_map()
    if not market_cap_map:
        print(
            "[funnel] ⚠️ 市值数据为空（TUSHARE_TOKEN 可能缺失/失效），Layer1 将跳过市值过滤"
        )
    print(f"[funnel] 加载股票名称...")
    name_map = _stock_name_map()

    # 大盘基准
    bench_df = None
    smallcap_df = None
    try:
        bench_df = fetch_index_hist("000001", start_s, end_s)
        print(f"[funnel] 大盘基准加载成功")
    except Exception as e:
        print(f"[funnel] 大盘基准加载失败: {e}")
    try:
        smallcap_df = fetch_index_hist(SMALLCAP_BENCH_CODE, start_s, end_s)
        print(f"[funnel] 小盘基准加载成功: {SMALLCAP_BENCH_CODE}")
    except Exception as e:
        print(f"[funnel] 小盘基准加载失败 {SMALLCAP_BENCH_CODE}: {e}")
    # 并发拉取日线（只负责取数，不负责计算）
    all_df_map: dict[str, pd.DataFrame] = {}
    fetch_ok = 0
    fetch_fail = 0
    fetch_date_mismatch = 0
    fetch_spot_patched = 0

    print(
        f"[funnel] 开始拉取 {len(all_symbols)} 只股票日线 "
        f"(executor={EXECUTOR_MODE}, batch_size={BATCH_SIZE}, max_workers={MAX_WORKERS}, batch_timeout={BATCH_TIMEOUT}s, "
        f"fetch_timeout={FETCH_TIMEOUT}s, retries={MAX_RETRIES})"
    )
    total_fetch_started = time.monotonic()
    for i in range(0, len(all_symbols), BATCH_SIZE):
        batch_no = i // BATCH_SIZE + 1
        batch = all_symbols[i : i + BATCH_SIZE]
        batch_ok = 0
        batch_fail = 0
        batch_started = time.monotonic()
        print(f"[funnel] 批次#{batch_no}/{total_batches} 启动，股票数={len(batch)}")

        use_process = EXECUTOR_MODE == "process"
        ex = (
            ProcessPoolExecutor(max_workers=MAX_WORKERS)
            if use_process
            else ThreadPoolExecutor(max_workers=MAX_WORKERS)
        )
        fetch_fn = (
            _fetch_one_with_retry if use_process else _fetch_one_with_retry_thread
        )
        futures = {ex.submit(fetch_fn, s, window): s for s in batch}
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
                    if ENFORCE_TARGET_TRADE_DATE:
                        latest_trade_date = _latest_trade_date_from_hist(df)
                        if latest_trade_date != window.end_trade_date:
                            df, patched = _append_spot_bar_if_needed(
                                sym,
                                df,
                                window.end_trade_date,
                            )
                            if patched:
                                latest_trade_date = _latest_trade_date_from_hist(df)
                                fetch_spot_patched += 1
                            batch_fail += 1
                            if latest_trade_date != window.end_trade_date:
                                fetch_fail += 1
                                fetch_date_mismatch += 1
                                print(
                                    f"[funnel] 批次#{batch_no} 跳过 {sym}: "
                                    f"latest_trade_date={latest_trade_date}, "
                                    f"target_trade_date={window.end_trade_date}"
                                )
                                continue
                            batch_fail -= 1
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
            if use_process:
                _terminate_executor_processes(ex, batch_no)
        finally:
            for ft in futures:
                ft.cancel()
            ex.shutdown(wait=False, cancel_futures=True)

        batch_elapsed = time.monotonic() - batch_started
        batch_qps = (batch_ok / batch_elapsed) if batch_elapsed > 0 else 0.0
        print(
            f"[funnel] 批次#{batch_no} 完成: 成功={batch_ok}, 失败={batch_fail}, "
            f"耗时={batch_elapsed:.1f}s, qps={batch_qps:.2f}, 累计成功={fetch_ok}, 累计失败={fetch_fail}"
        )
        if i + BATCH_SIZE < len(all_symbols) and BATCH_SLEEP > 0:
            time.sleep(BATCH_SLEEP)

    total_fetch_elapsed = time.monotonic() - total_fetch_started
    overall_qps = (fetch_ok / total_fetch_elapsed) if total_fetch_elapsed > 0 else 0.0
    print(
        f"[funnel] 日线拉取完成: 成功={fetch_ok}, 失败={fetch_fail}, "
        f"总耗时={total_fetch_elapsed:.1f}s, 平均qps={overall_qps:.2f}"
    )
    if ENFORCE_TARGET_TRADE_DATE:
        print(
            f"[funnel] 交易日对齐检查: mismatch={fetch_date_mismatch}, "
            f"spot_patched={fetch_spot_patched}, target_trade_date={window.end_trade_date}"
        )
    snapshot_dir = _dump_full_fetch_snapshot(
        df_map=all_df_map,
        all_symbols=all_symbols,
        window=window,
        fetch_stats={
            "fetch_ok": fetch_ok,
            "fetch_fail": fetch_fail,
            "fetch_date_mismatch": fetch_date_mismatch,
            "fetch_spot_patched": fetch_spot_patched,
            "fetch_elapsed_s": round(total_fetch_elapsed, 2),
            "fetch_qps": round(overall_qps, 3),
        },
        bench_df=bench_df,
        smallcap_df=smallcap_df,
    )

    # Step 0: 大盘总闸 + 全市场广度 + 动态阈值
    breadth_context = _calc_market_breadth(all_df_map, BREADTH_MA_WINDOW)
    benchmark_context = _analyze_benchmark_and_tune_cfg(
        bench_df,
        smallcap_df,
        cfg,
        breadth=breadth_context,
    )
    print(
        "[funnel] 大盘总闸: "
        f"regime={benchmark_context['regime']}, "
        f"close={benchmark_context['close']}, ma50={benchmark_context['ma50']}, ma200={benchmark_context['ma200']}, "
        f"ma50_slope_5d={benchmark_context['ma50_slope_5d']}, main_today={benchmark_context.get('main_today_pct')}, recent3={benchmark_context['recent3_pct']}, "
        f"recent3_cum={benchmark_context['recent3_cum_pct']}, "
        f"smallcap_code={benchmark_context.get('smallcap_code')}, smallcap_today={benchmark_context.get('smallcap_today_pct')}, "
        f"breadth={benchmark_context.get('breadth')}, "
        f"panic_triggered={benchmark_context.get('panic_triggered')}, panic_reasons={benchmark_context.get('panic_reasons')}, "
        f"repair_triggered={benchmark_context.get('repair_triggered')}, repair_reasons={benchmark_context.get('repair_reasons')}, "
        f"tuned={benchmark_context['tuned']}"
    )

    # 统一漏斗计算：L1 -> L2 -> L3 -> L4
    print(f"[funnel] 开始执行全量漏斗筛选...")

    # Layer 1
    l1_input = list(all_df_map.keys())
    l1_passed = layer1_filter(l1_input, name_map, market_cap_map, all_df_map, cfg)

    # Layer 2
    l2_passed, l2_channel_map = layer2_strength_detailed(l1_passed, all_df_map, bench_df, cfg)
    # 通道标签现在是多标签用 + 拼接，因此用 in 判断包含关系
    l2_momentum = sum(1 for v in l2_channel_map.values() if "主升通道" in v)
    l2_ambush   = sum(1 for v in l2_channel_map.values() if "潜伏通道" in v)
    l2_accum    = sum(1 for v in l2_channel_map.values() if "吸筹通道" in v)
    l2_dry_vol  = sum(1 for v in l2_channel_map.values() if "地量蓄势" in v)
    l2_rs_div   = sum(1 for v in l2_channel_map.values() if "暗中护盘" in v)
    l2_sos      = sum(1 for v in l2_channel_map.values() if "点火破局" in v)

    # Layer 3 (Sector Resonance)
    l3_passed, top_sectors = layer3_sector_resonance(
        l2_passed,
        sector_map,
        cfg,
        base_symbols=l1_passed,
        df_map=all_df_map,
    )
    sector_rotation = analyze_sector_rotation(
        all_df_map,
        sector_map,
        universe_symbols=list(all_df_map.keys()),
        focus_sectors=top_sectors,
    )
    benchmark_context["sector_rotation"] = sector_rotation
    print(f"[funnel] 板块轮动温度计: {sector_rotation.get('headline', '无')}")

    # Layer 4 (Wyckoff Triggers)
    # L4 需要 l2_df_map，这里直接用 all_df_map 即可，因为 key 都在里面
    triggers = layer4_triggers(l3_passed, all_df_map, cfg)

    # 新增：Markup 阶段、Accumulation ABC 细化、Exit 信号
    markup_symbols = detect_markup_stage(l3_passed, all_df_map, cfg)
    accum_stage_map = detect_accum_stage(l2_passed, all_df_map, cfg)
    exit_signals = layer5_exit_signals(l2_passed + markup_symbols, all_df_map, accum_stage_map, cfg)

    total_hits = sum(len(v) for v in triggers.values())
    ranked_l3_symbols, l3_score_map = _rank_l3_candidates(
        l3_symbols=l3_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        triggers=triggers,
        top_sectors=top_sectors,
        l2_channel_map=l2_channel_map,
        sector_rotation_map=(sector_rotation.get("state_map", {}) or {}),
    )
    metrics = {
        "total_symbols": len(all_symbols),
        "pool_mode": str(pool_stats.get("pool_mode", "") or ""),
        "pool_main": len(main_items),
        "pool_chinext": len(chinext_items),
        "pool_merged": len(merged_symbols),
        "pool_st_excluded": len(st_symbols),
        "pool_batches": total_batches,
        "fetch_ok": fetch_ok,
        "fetch_fail": fetch_fail,
        "fetch_date_mismatch": fetch_date_mismatch,
        "fetch_spot_patched": fetch_spot_patched,
        "snapshot_dir": snapshot_dir,
        "layer1": len(l1_passed),
        "layer2": len(l2_passed),
        "layer2_momentum": l2_momentum,
        "layer2_ambush": l2_ambush,
        "layer2_accum": l2_accum,
        "layer2_dry_vol": l2_dry_vol,
        "layer2_rs_div": l2_rs_div,
        "layer2_sos": l2_sos,
        "layer2_channel_map": l2_channel_map,
        "layer3": len(l3_passed),
        "top_sectors": top_sectors,
        "sector_rotation": sector_rotation,
        "layer3_symbols": ranked_l3_symbols or l3_passed,
        "layer3_score_map": l3_score_map,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
        "benchmark_context": benchmark_context,
        # 新增：阶段识别和退出信号
        "markup_symbols": markup_symbols,
        "accum_stage_map": accum_stage_map,
        "exit_signals": exit_signals,
    }
    if include_debug_context:
        metrics["_debug"] = {
            "cfg": cfg,
            "end_trade_date": window.end_trade_date.isoformat(),
            "all_symbols": all_symbols,
            "name_map": name_map,
            "market_cap_map": market_cap_map,
            "sector_map": sector_map,
            "bench_df": bench_df,
            "all_df_map": all_df_map,
            "layer1_symbols": l1_passed,
            "layer2_symbols": l2_passed,
            "layer3_symbols_raw": l3_passed,
        }
    print(
        f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
        f"(主升={l2_momentum}, 潜伏={l2_ambush}, 吸筹={l2_accum}, 地量={l2_dry_vol}, 护盘={l2_rs_div}, 点火={l2_sos}), "
        f"L3={metrics['layer3']}, 命中={total_hits}, "
        f"Top行业={top_sectors}, 各触发={metrics['by_trigger']}"
    )

    return triggers, metrics


def run(
    webhook_url: str,
    *,
    notify: bool = True,
    return_details: bool = False,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表, 大盘上下文)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job()
    benchmark_context = metrics.get("benchmark_context", {}) or {}
    name_map = _stock_name_map()
    sector_map = fetch_sector_map()

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
    use_legacy_selection = FUNNEL_AI_SELECTION_MODE in {
        "legacy_full_hits",
        "legacy_hits",
        "all_hits",
        "classic",
    }
    use_legacy_card = FUNNEL_CARD_STYLE in {
        "legacy",
        "legacy_compact",
        "classic",
        "v1",
    }
    l3_ranked_symbols = [
        str(c).strip()
        for c in (metrics.get("layer3_symbols", []) or [])
        if str(c).strip()
    ]
    l2_channel_map = metrics.get("layer2_channel_map", {}) or {}
    # 提前取出，供后面的闭包函数引用
    markup_symbols = metrics.get("markup_symbols", []) or []
    accum_stage_map = metrics.get("accum_stage_map", {}) or {}
    exit_signals = metrics.get("exit_signals", {}) or {}
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}
    # 策略：大盘水温驱动的双轨制（Top-Down 择时顺势策略）
    regime = benchmark_context.get("regime", "NEUTRAL")
    if use_legacy_selection:
        trend_selected = []
        accum_selected = []
        score_map = {c: float(code_to_best_score.get(c, 0.0)) for c in sorted_codes}
        ai_policy = {
            "total_cap": len(sorted_codes),
            "trend_quota": 0,
            "accum_quota": 0,
            "requested_trend_quota": 0,
            "requested_accum_quota": 0,
            "quota_family": "LEGACY_FULL_HITS",
            "max_trend_l3_fill": 0,
            "max_accum_l3_fill": 0,
        }
        selected_for_ai = list(sorted_codes)
        print(
            f"[funnel] AI候选分配完成(legacy_full_hits): total={len(selected_for_ai)}"
        )
    else:
        mock_result = FunnelResult(
            layer1_symbols=[],
            layer2_symbols=[],
            layer3_symbols=metrics.get("layer3_symbols", []) or [],
            top_sectors=[],
            triggers=triggers,
            stage_map=accum_stage_map,
            markup_symbols=markup_symbols,
            exit_signals=exit_signals,
            channel_map=l2_channel_map,
        )
        alloc_started = time.monotonic()
        trend_selected, accum_selected, score_map = allocate_ai_candidates(
            mock_result,
            l3_ranked_symbols,
            regime,
            sector_map=sector_map,
            max_per_sector=2,
        )
        ai_policy = resolve_ai_candidate_policy(regime)
        alloc_elapsed = time.monotonic() - alloc_started
        print(
            f"[funnel] AI候选分配完成: trend={len(trend_selected)}, accum={len(accum_selected)}, "
            f"elapsed={alloc_elapsed:.3f}s"
        )
        selected_for_ai = trend_selected + accum_selected

    if use_legacy_card and use_legacy_selection:
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
            reasons = "、".join(code_to_reasons.get(code, []))
            lines.append(
                f"• {code} {name} | {reasons} | score={code_to_best_score.get(code, 0):.2f}"
            )
        if not selected_for_ai:
            lines.append("无")

        content = "\n".join(lines)
        title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
        ok = True if not notify else send_feishu_notification(webhook_url, title, content)

        sos_hit_set = set(str(c).strip() for c, _ in triggers.get("sos", []))
        evr_hit_set = set(str(c).strip() for c, _ in triggers.get("evr", []))
        spring_hit_set = set(str(c).strip() for c, _ in triggers.get("spring", []))
        lps_hit_set = set(str(c).strip() for c, _ in triggers.get("lps", []))

        def _infer_track(code: str) -> str:
            if code in sos_hit_set or code in evr_hit_set:
                return "Trend"
            if code in spring_hit_set or code in lps_hit_set:
                return "Accum"
            return "Trend"

        def _legacy_stage(code: str) -> str:
            if code in markup_symbols:
                return "Markup"
            return str(accum_stage_map.get(code, "") or "").strip()

        symbols_for_report = [
            {
                "code": c,
                "name": name_map.get(c, c),
                "tag": "、".join(code_to_reasons.get(c, [])),
                "track": _infer_track(c),
                "stage": _legacy_stage(c),
                "score": float((metrics.get("layer3_score_map", {}) or {}).get(c, 0.0)),
                "priority_score": float(code_to_best_score.get(c, 0.0)),
                "priority_rank": idx + 1,
                "selection_source": "l4_hit",
                "selection_is_fill": False,
                "industry": str(sector_map.get(c, "") or "未知行业"),
                "sector_state_code": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
                ).strip(),
                "sector_state": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                        "label",
                        "",
                    )
                ).strip(),
                "sector_note": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
                ).strip(),
                "sector_guidance": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                        "guidance", ""
                    )
                ).strip(),
                "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
                "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
                "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
            }
            for idx, c in enumerate(selected_for_ai)
        ]
        if return_details:
            details = {
                "metrics": metrics,
                "triggers": triggers,
                "content": content,
                "title": title,
                "symbols_for_report": symbols_for_report,
                "selected_for_ai": selected_for_ai,
                "trend_selected": [],
                "accum_selected": [],
                "priority_score_map": score_map,
                "name_map": name_map,
                "sector_map": sector_map,
            }
            return (ok, symbols_for_report, benchmark_context, details)
        return (ok, symbols_for_report, benchmark_context)
    
    def _channel_tags(code: str) -> set[str]:
        raw = str(l2_channel_map.get(code, "")).strip()
        if not raw:
            return set()
        return {x.strip() for x in raw.split("+") if x.strip()}

    hit_set = set(sorted_codes)
    sos_hit_set = set(str(c).strip() for c, _ in triggers.get("sos", []))
    spring_hit_set = set(str(c).strip() for c, _ in triggers.get("spring", []))
    lps_hit_set = set(str(c).strip() for c, _ in triggers.get("lps", []))
    evr_hit_set = set(str(c).strip() for c, _ in triggers.get("evr", []))

    def _stage_name(code: str) -> str:
        if code in markup_symbols:
            return "Markup"
        return str(accum_stage_map.get(code, "") or "").strip()

    hit_selected_count = sum(1 for c in selected_for_ai if c in hit_set)
    l3_only_count = len(selected_for_ai) - hit_selected_count
    trend_hit_selected = sum(1 for c in trend_selected if c in hit_set)
    trend_l3_only = len(trend_selected) - trend_hit_selected
    accum_hit_selected = sum(1 for c in accum_selected if c in hit_set)
    accum_l3_only = len(accum_selected) - accum_hit_selected

    channel_counts = {
        "主升通道": 0,
        "潜伏通道": 0,
        "吸筹通道": 0,
        "地量蓄势": 0,
        "暗中护盘": 0,
        "点火破局": 0,
    }
    for code in selected_for_ai:
        tags = _channel_tags(code)
        for key in channel_counts.keys():
            if key in tags:
                channel_counts[key] += 1
    l3_score_map = metrics.get("layer3_score_map", {}) or {}
    by_trigger = metrics.get("by_trigger", {}) or {}
    l2_momentum = int(metrics.get("layer2_momentum", 0) or 0)
    l2_ambush   = int(metrics.get("layer2_ambush", 0) or 0)
    l2_accum    = int(metrics.get("layer2_accum", 0) or 0)
    l2_dry_vol  = int(metrics.get("layer2_dry_vol", 0) or 0)
    l2_rs_div   = int(metrics.get("layer2_rs_div", 0) or 0)
    l2_sos      = int(metrics.get("layer2_sos", 0) or 0)
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}
    markup_count = len(markup_symbols)
    accum_a_count = sum(1 for v in accum_stage_map.values() if v == "Accum_A")
    accum_b_count = sum(1 for v in accum_stage_map.values() if v == "Accum_B")
    accum_c_count = sum(1 for v in accum_stage_map.values() if v == "Accum_C")
    stop_loss_count = sum(
        1 for sig in exit_signals.values() if sig.get("signal") == "stop_loss"
    )
    dist_warning_count = sum(
        1 for sig in exit_signals.values() if sig.get("signal") == "distribution_warning"
    )
    blocked_exit_signals_set = {"stop_loss", "distribution_warning"}
    blocked_exit_codes = [
        code for code in l3_ranked_symbols
        if str((exit_signals.get(code, {}) or {}).get("signal", "")).strip() in blocked_exit_signals_set
    ]

    total_cap = int(ai_policy["total_cap"])
    trend_quota = int(ai_policy["trend_quota"])
    accum_quota = int(ai_policy["accum_quota"])
    requested_trend_quota = int(ai_policy["requested_trend_quota"])
    requested_accum_quota = int(ai_policy["requested_accum_quota"])
    quota_family = str(ai_policy["quota_family"])
    max_trend_l3_fill = int(ai_policy["max_trend_l3_fill"])
    max_accum_l3_fill = int(ai_policy["max_accum_l3_fill"])

    print(
        f"[funnel] 候选分层: 命中事件={metrics['total_hits']}, 命中股票={unique_hit_count}, "
        f"配额配置=[{regime}->{quota_family}: requested Trend={requested_trend_quota}, "
        f"requested Accum={requested_accum_quota}, effective Trend={trend_quota}, "
        f"effective Accum={accum_quota}, 总上限={total_cap}, "
        f"l3_fill_limit Trend={max_trend_l3_fill}, Accum={max_accum_l3_fill}], "
        f"最终选入: Trend={len(trend_selected)}, Accum={len(accum_selected)}, 总计={len(selected_for_ai)}"
    )

    bench_line = "未知"
    if benchmark_context:
        breadth = benchmark_context.get("breadth", {}) or {}
        breadth_text = (
            f"，上涨家数占比 {breadth.get('ratio_pct'):.1f}%"
            f"（前日 {breadth.get('prev_ratio_pct'):.1f}%，变化 {breadth.get('delta_pct'):+.1f}%，样本 {breadth.get('sample_size')} 只）"
            if breadth
            else ""
        )
        repair_text = (
            f"，修复原因：{benchmark_context.get('repair_reasons')}"
            if benchmark_context.get("repair_triggered")
            else ""
        )
        smallcap_close = benchmark_context.get("smallcap_close")
        smallcap_cum3 = benchmark_context.get("smallcap_recent3_cum_pct")
        smallcap_text = (
            f" | 创业板指 {smallcap_close:.2f}，近3日 {smallcap_cum3:+.2f}%"
            if smallcap_close is not None and smallcap_cum3 is not None
            else ""
        )
        bench_line = (
            f"{benchmark_context.get('regime')} | 沪深300 {benchmark_context.get('close'):.2f}"
            f"（MA50={benchmark_context.get('ma50'):.1f} MA200={benchmark_context.get('ma200'):.1f}）"
            f"，近3日 {benchmark_context.get('recent3_cum_pct'):+.2f}%"
            f"{smallcap_text}"
            f"{breadth_text}{repair_text}"
        )

    data_quality_line = (
        f"成功拉取 {metrics['fetch_ok']} 只"
        + (f"，失败 {metrics['fetch_fail']} 只" if metrics['fetch_fail'] else "，无失败")
        + (f"，日期不对齐跳过 {metrics.get('fetch_date_mismatch', 0)} 只" if metrics.get('fetch_date_mismatch') else "")
        + (f"，实时快照补偿 {metrics.get('fetch_spot_patched', 0)} 只" if metrics.get('fetch_spot_patched') else "")
    )
    ai_channel_summary = " | ".join(
        f"{k}{channel_counts[k]}"
        for k in ["主升通道", "潜伏通道", "吸筹通道", "地量蓄势", "暗中护盘", "点火破局"]
        if channel_counts[k] > 0
    ) or "无"
    l4_non_hit_count = max(int(metrics["layer3"]) - int(unique_hit_count), 0)
    top_priority_count = sum(
        1 for c in selected_for_ai if c in markup_symbols or c in sos_hit_set or c in spring_hit_set
    )

    lines = [
        "## 一览",
        (
            f"- **股票池**：主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
            f"→ 去重{metrics['pool_merged']} → 去ST{metrics['pool_st_excluded']} "
            f"= **{metrics['total_symbols']}**（共{metrics['pool_batches']}批）"
        ),
        f"- **大盘水温**：{bench_line}",
        f"- **Top 行业**：{', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        f"- **板块轮动温度计**：{sector_rotation.get('headline', '无')}",
        f"- **数据质量**：{data_quality_line}",
        "",
        "## 漏斗进度",
        f"- **L1 通过**：{metrics['layer1']} / {metrics['total_symbols']}（剔除 {metrics['total_symbols'] - metrics['layer1']}）",
        f"- **L2 通过**：{metrics['layer2']} / {metrics['layer1']}（至少满足一条二级通道）",
        f"- **L3 保留**：{metrics['layer3']} / {metrics['layer2']}（当前仅做行业标记，不做硬剔除）",
        f"- **L4 命中股票**：{unique_hit_count} 只（命中事件 {metrics['total_hits']} 次）",
        f"- **L4 未命中**：{l4_non_hit_count} 只（仍留在 L3 观察池）",
        "",
        "## L2 通道与阶段",
        f"- **L2 通道分布**：主升{l2_momentum} | 潜伏{l2_ambush} | 吸筹{l2_accum} | 地量{l2_dry_vol} | 护盘{l2_rs_div} | 点火{l2_sos}",
        f"- **威科夫阶段**：Markup{markup_count} | Accum_A{accum_a_count} | Accum_B{accum_b_count} | Accum_C{accum_c_count}",
        f"- **板块轮动状态**：分歧{int((sector_rotation.get('counts', {}) or {}).get('DISAGREEMENT_PULLBACK', 0))} | 健康{int((sector_rotation.get('counts', {}) or {}).get('HEALTHY_MAINLINE', 0))} | 高潮{int((sector_rotation.get('counts', {}) or {}).get('CONSENSUS_CLIMAX', 0))} | 退潮{int((sector_rotation.get('counts', {}) or {}).get('DISTRIBUTION_RISK', 0))}",
        "",
        "## L4 形态触发",
        f"- **SOS（量价点火）**：{len(sos_hit_set)}",
        f"- **Spring（终极震仓）**：{len(spring_hit_set)}",
        f"- **LPS（缩量回踩）**：{len(lps_hit_set)}",
        f"- **EVR（放量不跌）**：{len(evr_hit_set)}",
        "",
        "## 风控与 AI 筛后",
        f"- **Exit 参考信号**：结构止损{stop_loss_count} | Distribution警告{dist_warning_count}",
        f"- **硬剔除**：{len(blocked_exit_codes)} 只（已触发结构止损或派发警告，不再送入 AI）",
        (
            f"- **最终送 AI**：{len(selected_for_ai)} 只"
            f"（{regime}->{quota_family}：Trend={trend_quota} / Accum={accum_quota} / 总上限={total_cap}）"
        ),
        f"- **AI 入选构成**：L4命中 {hit_selected_count} | L3补充 {l3_only_count}",
        f"- **Trend 轨**：{len(trend_selected)} 只（L4命中 {trend_hit_selected} | L3补充 {trend_l3_only}）",
        f"- **Accum 轨**：{len(accum_selected)} 只（L4命中 {accum_hit_selected} | L3补充 {accum_l3_only}）",
        f"- **L3 补位上限**：Trend {max_trend_l3_fill} | Accum {max_accum_l3_fill}",
        f"- **高优先级候选**：{top_priority_count} 只",
        f"- **AI 输入通道分布**：{ai_channel_summary}",
    ]
    rotation_overview_lines = sector_rotation.get("overview_lines", []) or []
    if rotation_overview_lines:
        lines.extend(["", "## 板块轮动水温计"])
        lines.extend([f"- {x}" for x in rotation_overview_lines])

    def _append_ai_section(
        lines_obj: list[str], section_title: str, section_desc: str, codes: list[str]
    ) -> None:
        lines_obj.extend(
            [
                "",
                section_title,
                f"- **说明**：{section_desc}",
                "- **字段**：代码 名称 | 阶段 | 来源标签 | 风控提示 | 分值",
                "",
            ]
        )
        if not codes:
            lines_obj.append("- 无")
            return
        for code in codes:
            name = name_map.get(code, code)
            trigger_reason = "、".join(code_to_reasons.get(code, []))
            channel = str(l2_channel_map.get(code, "")).strip()
            industry = str(sector_map.get(code, "") or "未知行业")
            sector_info = sector_rotation_map.get(industry, {}) or {}
            sector_state_label = str(
                sector_info.get("label", SECTOR_STATE_LABELS.get("NEUTRAL_MIXED", "中性混沌"))
            ).strip()
            stage = accum_stage_map.get(code, "")
            if not stage and code in markup_symbols:
                stage = "Markup"
            stage_str = f"[{stage}]" if stage else ""
            base_reason = trigger_reason or "威科夫候选"
            sector_reason = f"板块:{sector_state_label}"
            if channel:
                reasons = f"{channel} | {sector_reason} | {base_reason}"
            else:
                reasons = f"{sector_reason} | {base_reason}"

            exit_sig = exit_signals.get(code, {})
            exit_str = ""
            if exit_sig.get("signal") == "stop_loss":
                exit_str = f"| ✗止损{exit_sig.get('price', 0):.2f}"
            elif exit_sig.get("signal") == "distribution_warning":
                exit_str = "| ⚠Distribution警告"

            priority_score = float(score_map.get(code, 0.0))
            l3_score = float(l3_score_map.get(code, 0.0))
            lines_obj.append(
                f"- {code} {name} {stage_str} | {reasons} {exit_str} | priority={priority_score:.2f}, l3={l3_score:.2f}"
            )

    _append_ai_section(
        lines,
        "## AI 输入·Trend轨",
        "右侧主升，优先 SOS（量价点火）与 Markup 阶段。",
        trend_selected,
    )
    _append_ai_section(
        lines,
        "## AI 输入·Accum轨",
        "左侧潜伏，优先 Spring（终极震仓）/LPS（缩量回踩）与 Accum_C 阶段。",
        accum_selected,
    )

    if not selected_for_ai:
        lines.extend(
            [
                "",
                "**为什么没候选**",
                f"• 触发信号: SOS={int(by_trigger.get('sos', 0))}, Spring={int(by_trigger.get('spring', 0))}, LPS={int(by_trigger.get('lps', 0))}, EVR={int(by_trigger.get('evr', 0))}",
                f"• 阶段分布: Markup={markup_count}, Accum_A={accum_a_count}, Accum_B={accum_b_count}, Accum_C={accum_c_count}",
                f"• 水温判断: {regime} | 当前配额: Trend={trend_quota}, Accum={accum_quota}, 总上限={total_cap}",
                "• 分析：候选股票尚未达到日线级别的威科夫触发信号（SOS/Spring/LPS）或阶段转折特征。" if total_cap > 0 else "• 当前大盘水温，AI配额已关闭（total_cap=0）。",
            ]
        )

    content = "\n".join(lines)
    title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
    ok = True if not notify else send_feishu_notification(webhook_url, title, content)

    def _selection_source(code: str) -> str:
        if code in hit_set:
            return "l4_hit"
        if code in markup_symbols:
            return "markup"
        if _stage_name(code) == "Accum_C":
            return "accum_c"
        return "l3_fill"

    symbols_for_report = [
        {
            "code": c,
            "name": name_map.get(c, c),
            "tag": (
                f"{str(l2_channel_map.get(c, '')).strip()} | "
                f"{'、'.join(code_to_reasons.get(c, [])) or '威科夫候选'}"
            ).strip(" |"),
            "track": (
                "Trend"
                if c in trend_selected
                else "Accum" if c in accum_selected else ""
            ),
            "stage": _stage_name(c),
            "score": float(l3_score_map.get(c, 0.0)),
            "priority_score": float(score_map.get(c, 0.0)),
            "priority_rank": idx + 1,
            "selection_source": _selection_source(c),
            "selection_is_fill": _selection_source(c) == "l3_fill",
            "industry": str(sector_map.get(c, "") or "未知行业"),
            "sector_state_code": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
            ).strip(),
            "sector_state": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                    "label",
                    "",
                )
            ).strip(),
            "sector_note": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
            ).strip(),
            "sector_guidance": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("guidance", "")
            ).strip(),
            "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
            "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
            "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
        }
        for idx, c in enumerate(selected_for_ai)
    ]
    if return_details:
        details = {
            "metrics": metrics,
            "triggers": triggers,
            "content": content,
            "title": title,
            "symbols_for_report": symbols_for_report,
            "selected_for_ai": selected_for_ai,
            "trend_selected": trend_selected,
            "accum_selected": accum_selected,
            "priority_score_map": score_map,
            "name_map": name_map,
            "sector_map": sector_map,
        }
        return (ok, symbols_for_report, benchmark_context, details)
    return (ok, symbols_for_report, benchmark_context)
