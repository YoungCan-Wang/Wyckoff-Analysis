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
    layer2_strength,
    layer3_sector_resonance,
    layer4_triggers,
    normalize_hist_from_fetch,
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
PANIC_SNAPSHOT_TIMEOUT = max(
    float(os.getenv("FUNNEL_PANIC_SNAPSHOT_TIMEOUT", "25.0")),
    1.0,
)
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
CRASH_EXTREME_DROP_PCT = float(os.getenv("FUNNEL_CRASH_EXTREME_DROP_PCT", "-9.0"))
CRASH_EXTREME_DROP_COUNT = int(os.getenv("FUNNEL_CRASH_EXTREME_DROP_COUNT", "50"))
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
PANIC_REPAIR_MAX_EXTREME_COUNT = int(
    os.getenv("FUNNEL_PANIC_REPAIR_MAX_EXTREME_COUNT", "20")
)
FUNNEL_EXPORT_FULL_FETCH = os.getenv("FUNNEL_EXPORT_FULL_FETCH", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_EXPORT_DIR = os.getenv("FUNNEL_EXPORT_DIR", "data/funnel_snapshots").strip() or "data/funnel_snapshots"


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _apply_funnel_cfg_overrides(cfg: FunnelConfig) -> None:
    """
    将 .env 中的 FUNNEL_CFG_* 参数映射到 FunnelConfig。
    示例：FUNNEL_CFG_MIN_MARKET_CAP_YI=20
    """
    for f in dataclass_fields(FunnelConfig):
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
            volume_f = 0.0
            amount_f = 0.0
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
    panic_snapshot: dict | None = None,
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
        "panic_snapshot": {
            "ok": False,
            "total": 0,
            "down_extreme_count": 0,
            "down_9_count": 0,
            "down_10_count": 0,
            "up_9_count": 0,
        },
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
    snap = panic_snapshot or {}
    snap_ok = bool(snap.get("ok"))
    snap_down_extreme = int(snap.get("down_extreme_count") or 0)
    snap_down_9 = int(snap.get("down_9_count") or 0)
    extreme_snapshot_panic = (
        snap_ok
        and CRASH_EXTREME_DROP_COUNT > 0
        and snap_down_extreme >= CRASH_EXTREME_DROP_COUNT
    )
    if extreme_snapshot_panic:
        panic_reasons.append(
            f"down_{CRASH_EXTREME_DROP_PCT:g}_count={snap_down_extreme}>=阈值{CRASH_EXTREME_DROP_COUNT}"
        )

    repair_reasons: list[str] = []
    if extreme_snapshot_panic:
        regime = "BLACK_SWAN"
    elif panic_reasons:
        regime = "CRASH"
    elif PANIC_REPAIR_ENABLE:
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
        extreme_ok = (not snap_ok) or (
            snap_down_extreme <= max(PANIC_REPAIR_MAX_EXTREME_COUNT, 0)
        )
        if prev_panic and rebound_ok and extreme_ok:
            regime = "PANIC_REPAIR"
            repair_reasons = [
                f"prev_panic(main_prev={main_prev_pct}, small_prev={small_prev_pct})",
                f"rebound_ok(main_today={main_today_pct}, small_today={small_today_pct})",
                (
                    f"down_{CRASH_EXTREME_DROP_PCT:g}_count={snap_down_extreme}<=阈值{PANIC_REPAIR_MAX_EXTREME_COUNT}"
                    if snap_ok
                    else "snapshot_unavailable"
                ),
            ]

    # 动态调参：风险越冷，过滤越严
    if regime == "BLACK_SWAN":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 30000.0)
        cfg.rs_min_long = max(cfg.rs_min_long, 8.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 2.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 95.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 90.0)
    elif regime == "CRASH":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 18000.0)
        cfg.rs_min_long = max(cfg.rs_min_long, 4.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 1.0)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 90.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 85.0)
    elif regime == "PANIC_REPAIR":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 8000.0)
        cfg.rs_min_long = max(cfg.rs_min_long, 1.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.2)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 75.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 65.0)
    elif regime == "RISK_OFF":
        cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 10000.0)
        cfg.rs_min_long = max(cfg.rs_min_long, 2.0)
        cfg.rs_min_short = max(cfg.rs_min_short, 0.5)
        cfg.rps_fast_min = max(cfg.rps_fast_min, 80.0)
        cfg.rps_slow_min = max(cfg.rps_slow_min, 75.0)
        if recent3_cum is not None and recent3_cum <= -4.0:
            cfg.min_avg_amount_wan = max(cfg.min_avg_amount_wan, 15000.0)
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
            "panic_snapshot": {
                "ok": snap_ok,
                "total": int(snap.get("total") or 0),
                "down_extreme_count": snap_down_extreme,
                "down_9_count": snap_down_9,
                "down_10_count": int(snap.get("down_10_count") or 0),
                "up_9_count": int(snap.get("up_9_count") or 0),
            },
            "tuned": {
                "min_avg_amount_wan": cfg.min_avg_amount_wan,
                "rs_min_long": cfg.rs_min_long,
                "rs_min_short": cfg.rs_min_short,
                "rps_fast_min": cfg.rps_fast_min,
                "rps_slow_min": cfg.rps_slow_min,
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


def _extract_spot_snapshot_trade_date(df: pd.DataFrame) -> date | None:
    """
    尝试从实时快照表中提取“行情日期”。
    不同数据源/版本列名可能不同，这里做 best-effort 兼容。
    """
    if df is None or df.empty:
        return None
    candidates = (
        "数据日期",
        "日期",
        "更新时间",
        "最新时间",
        "行情时间",
        "时间",
    )
    for col in candidates:
        if col not in df.columns:
            continue
        ts = pd.to_datetime(df[col], errors="coerce")
        ts = ts.dropna()
        if ts.empty:
            continue
        ts = ts[ts.dt.year >= 2000]
        if ts.empty:
            continue
        return ts.max().date()
    return None


def _fetch_market_extreme_snapshot(target_trade_date: date | None = None) -> dict:
    """
    快照级市场恐慌探针（不依赖全量日线拉取完成）：
    - down_extreme_count: 涨跌幅 <= CRASH_EXTREME_DROP_PCT 数量
    - down_9_count: 涨跌幅 <= -9.0% 数量
    - down_10_count: 涨跌幅 <= -10.0% 数量
    - up_9_count: 涨跌幅 >= 9.0% 数量

    防错位约束：
    - 当 target_trade_date 不是“今天”时，跳过快照（避免把历史目标日与当下快照混用）。
    - 若快照含日期列，要求其日期与 target_trade_date 一致。
    """
    out = {
        "ok": False,
        "total": 0,
        "down_extreme_count": 0,
        "down_9_count": 0,
        "down_10_count": 0,
        "up_9_count": 0,
        "snapshot_trade_date": None,
    }
    timeout_s = PANIC_SNAPSHOT_TIMEOUT
    now_cn = datetime.now(CN_TZ)
    if target_trade_date is not None:
        if target_trade_date != now_cn.date():
            print(
                "[funnel] 市场极端快照跳过: "
                f"target_trade_date={target_trade_date}, today={now_cn.date()}"
            )
            return out
        if (now_cn.hour, now_cn.minute) < (9, 15):
            print(
                "[funnel] 市场极端快照跳过: 当前早于 09:15，"
                "避免读取隔夜残留快照"
            )
            return out
    try:
        import akshare as ak

        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(ak.stock_zh_a_spot_em)
            df = fut.result(timeout=max(timeout_s, 1.0))
        if df is None or df.empty:
            return out
        snap_trade_date = _extract_spot_snapshot_trade_date(df)
        if snap_trade_date is not None:
            out["snapshot_trade_date"] = snap_trade_date.isoformat()
        if (
            target_trade_date is not None
            and snap_trade_date is not None
            and snap_trade_date != target_trade_date
        ):
            print(
                "[funnel] 市场极端快照日期不匹配: "
                f"snapshot_date={snap_trade_date}, target_trade_date={target_trade_date}"
            )
            return out
        pct = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
        if pct is None or pct.dropna().empty:
            return out
        out["ok"] = True
        out["total"] = int(pct.dropna().shape[0])
        out["down_extreme_count"] = int((pct <= CRASH_EXTREME_DROP_PCT).sum())
        out["down_9_count"] = int((pct <= -9.0).sum())
        out["down_10_count"] = int((pct <= -10.0).sum())
        out["up_9_count"] = int((pct >= 9.0).sum())
        return out
    except FuturesTimeoutError:
        print(f"[funnel] 市场极端快照超时: >{timeout_s:.1f}s")
        return out
    except Exception as e:
        print(f"[funnel] 市场极端快照获取失败: {e}")
        return out


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


def _rank_l3_watchlist(
    l3_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    triggers: dict[str, list[tuple[str, float]]],
    top_sectors: list[str],
    top_k: int = 15,
) -> tuple[list[str], list[dict], dict[str, float], list[dict]]:
    """
    对 L3 股票做统一优先级排序，并给出 TopK 自选建议。
    评分构成（分位）：
    - 20日收益强度 40%
    - 5日收益强度 25%
    - 最近5日最小量比（越小越好）20%
    - 威科夫触发强度 15%
    - Top行业额外加分 0.05
    """
    if not l3_symbols:
        return ([], [], {}, [])

    trigger_score_map: dict[str, float] = {}
    trigger_reason_map: dict[str, list[str]] = {}
    for key, label in TRIGGER_LABELS.items():
        for code, score in triggers.get(key, []):
            trigger_score_map[code] = max(trigger_score_map.get(code, 0.0), float(score))
            trigger_reason_map.setdefault(code, [])
            if label not in trigger_reason_map[code]:
                trigger_reason_map[code].append(label)

    rows: list[dict] = []
    for code in l3_symbols:
        df = df_map.get(code)
        industry = str(sector_map.get(code, "") or "未知行业")
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
                "reasons": "、".join(trigger_reason_map.get(code, [])),
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
    rank_df["watch_score"] = (
        0.40 * rank_df["q20"]
        + 0.25 * rank_df["q5"]
        + 0.20 * rank_df["dry_q"]
        + 0.15 * rank_df["trigger_q"]
        + rank_df["hot_bonus"]
    )

    rank_df = rank_df.sort_values("watch_score", ascending=False).reset_index(drop=True)
    ranked_symbols = rank_df["code"].astype(str).tolist()
    score_map = {
        str(r["code"]): float(r["watch_score"])
        for _, r in rank_df.iterrows()
    }

    def _fnum(v: object, nd: int = 2, fallback: str = "-") -> str:
        try:
            x = float(v)  # type: ignore[arg-type]
        except Exception:
            return fallback
        if pd.isna(x):
            return fallback
        return f"{x:.{nd}f}"

    ranked_rows: list[dict] = []
    for _, r in rank_df.iterrows():
        trigger_reason = str(r.get("reasons", "")).strip()
        industry = str(r.get("industry", "")).strip() or "未知行业"
        hot_bonus = float(r.get("hot_bonus", 0.0))
        ret20 = r.get("ret20")
        ret5 = r.get("ret5")
        dry_ratio = r.get("min_vol_ratio_5d")
        parts: list[str] = []
        if hot_bonus > 0:
            parts.append("Top行业共振")
        else:
            parts.append("行业共振通过")
        if trigger_reason:
            parts.append(f"L4痕迹:{trigger_reason}")
        else:
            parts.append("未触发L4扳机")
        try:
            dry_v = float(dry_ratio)  # type: ignore[arg-type]
            if not pd.isna(dry_v) and dry_v <= 1.0:
                parts.append(f"近5日有缩量(最小量比{dry_v:.2f})")
        except Exception:
            pass
        parts.append(
            f"20日动量{_fnum(ret20, 1, '0.0')}%/5日{_fnum(ret5, 1, '0.0')}%"
        )
        ranked_rows.append(
            {
                "code": str(r["code"]),
                "industry": industry,
                "reason": "；".join(parts),
                "score": float(r["watch_score"]),
                "ret20": (float(ret20) if pd.notna(pd.to_numeric(ret20, errors='coerce')) else None),
                "ret5": (float(ret5) if pd.notna(pd.to_numeric(ret5, errors='coerce')) else None),
                "min_vol_ratio_5d": (
                    float(dry_ratio)
                    if pd.notna(pd.to_numeric(dry_ratio, errors="coerce"))
                    else None
                ),
                "trigger_labels": trigger_reason,
            }
        )

    top_rows: list[dict] = []
    for _, r in rank_df.head(max(int(top_k), 0)).iterrows():
        code = str(r["code"])
        reason = "L3共振通过"
        for row in ranked_rows:
            if row.get("code") == code:
                reason = str(row.get("reason", "")).strip() or reason
                break
        top_rows.append(
            {
                "code": code,
                "industry": str(r.get("industry", "")),
                "reason": reason,
                "score": float(r["watch_score"]),
            }
        )
    return (ranked_symbols, top_rows, score_map, ranked_rows)


def run_funnel_job() -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    _apply_funnel_cfg_overrides(cfg)
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
        sym
        for sym in merged_symbols
        if "ST" in merged_code_to_name.get(sym, "").upper()
    ]
    st_set = set(st_symbols)
    all_symbols = [sym for sym in merged_symbols if sym not in st_set]
    total_batches = (
        (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    )
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
    panic_snapshot = _fetch_market_extreme_snapshot(
        target_trade_date=window.end_trade_date
    )
    if panic_snapshot.get("ok"):
        snap_date = panic_snapshot.get("snapshot_trade_date")
        snap_date_prefix = f"date={snap_date}, " if snap_date else ""
        print(
            f"[funnel] 市场极端快照: {snap_date_prefix}"
            f"total={panic_snapshot.get('total')}, "
            f"down_extreme({CRASH_EXTREME_DROP_PCT:g})={panic_snapshot.get('down_extreme_count')}, "
            f"down_9={panic_snapshot.get('down_9_count')}, "
            f"down_10={panic_snapshot.get('down_10_count')}, "
            f"up_9={panic_snapshot.get('up_9_count')}"
        )
    else:
        print("[funnel] 市场极端快照: unavailable")

    # 并发拉取日线（只负责取数，不负责计算）
    all_df_map: dict[str, pd.DataFrame] = {}
    fetch_ok = 0
    fetch_fail = 0
    fetch_date_mismatch = 0
    fetch_spot_patched = 0

    print(
        f"[funnel] 开始拉取 {len(all_symbols)} 只股票日线 "
        f"(executor={EXECUTOR_MODE}, batch_size={BATCH_SIZE}, max_workers={MAX_WORKERS}, batch_timeout={BATCH_TIMEOUT}s, "
        f"fetch_timeout={FETCH_TIMEOUT}s, panic_snapshot_timeout={PANIC_SNAPSHOT_TIMEOUT:.1f}s, retries={MAX_RETRIES})"
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
        panic_snapshot=panic_snapshot,
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
        f"panic_snapshot={benchmark_context.get('panic_snapshot')}, "
        f"tuned={benchmark_context['tuned']}"
    )

    # 统一漏斗计算：L1 -> L2 -> L3 -> L4
    print(f"[funnel] 开始执行全量漏斗筛选...")

    # Layer 1
    l1_input = list(all_df_map.keys())
    l1_passed = layer1_filter(l1_input, name_map, market_cap_map, all_df_map, cfg)

    # Layer 2
    l2_passed = layer2_strength(l1_passed, all_df_map, bench_df, cfg)

    # Layer 3 (Sector Resonance)
    l3_passed, top_sectors = layer3_sector_resonance(
        l2_passed,
        sector_map,
        cfg,
        base_symbols=l1_passed,
        df_map=all_df_map,
    )

    # Layer 4 (Wyckoff Triggers)
    # L4 需要 l2_df_map，这里直接用 all_df_map 即可，因为 key 都在里面
    triggers = layer4_triggers(l3_passed, all_df_map, cfg)

    total_hits = sum(len(v) for v in triggers.values())
    ranked_l3_symbols, watchlist_top15, l3_score_map, l3_ranked_rows = _rank_l3_watchlist(
        l3_symbols=l3_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        triggers=triggers,
        top_sectors=top_sectors,
        top_k=15,
    )
    metrics = {
        "total_symbols": len(all_symbols),
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
        "layer3": len(l3_passed),
        "top_sectors": top_sectors,
        "layer3_symbols": ranked_l3_symbols or l3_passed,
        "layer3_score_map": l3_score_map,
        "layer3_ranked_rows": l3_ranked_rows,
        "watchlist_top15": watchlist_top15,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
        "benchmark_context": benchmark_context,
    }
    print(
        f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
        f"L3={metrics['layer3']}, 命中={total_hits}, "
        f"Top行业={top_sectors}, 各触发={metrics['by_trigger']}"
    )

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
    l3_score_map = metrics.get("layer3_score_map", {}) or {}
    l3_ranked_rows = metrics.get("layer3_ranked_rows", []) or []
    watchlist_top15 = metrics.get("watchlist_top15", []) or []
    by_trigger = metrics.get("by_trigger", {}) or {}

    print(
        f"[funnel] 候选分层: 命中事件={metrics['total_hits']}, 命中股票={unique_hit_count}, "
        f"AI输入=hits全量{len(selected_for_ai)}, "
        f"AI分析={len(selected_for_ai)}, Top15自选={len(watchlist_top15)}"
    )

    bench_line = "未知"
    if benchmark_context:
        breadth = benchmark_context.get("breadth", {}) or {}
        panic_snapshot = benchmark_context.get("panic_snapshot", {}) or {}
        breadth_text = (
            f"，上涨家数占比 {breadth.get('ratio_pct'):.1f}%"
            f"（前日 {breadth.get('prev_ratio_pct'):.1f}%，变化 {breadth.get('delta_pct'):+.1f}%，样本 {breadth.get('sample_size')} 只）"
            if breadth
            else ""
        )
        panic_text = (
            f"，恐慌信号：极端下跌({CRASH_EXTREME_DROP_PCT:g}%) {panic_snapshot.get('down_extreme_count')} 只"
            f" / 跌9% {panic_snapshot.get('down_9_count')} 只"
            f" / 涨停 {panic_snapshot.get('up_9_count')} 只"
            if panic_snapshot.get("ok")
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
            f"{breadth_text}{panic_text}{repair_text}"
        )

    lines = [
        (
        f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
        f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
        f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
        ),
        f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 命中:{metrics['total_hits']}",
        (
        f"**数据质量**: 成功拉取 {metrics['fetch_ok']} 只"
        + (f"，失败 {metrics['fetch_fail']} 只" if metrics['fetch_fail'] else "，无失败")
        + (f"，日期不对齐跳过 {metrics.get('fetch_date_mismatch', 0)} 只" if metrics.get('fetch_date_mismatch') else "")
        + (f"，实时快照补偿 {metrics.get('fetch_spot_patched', 0)} 只" if metrics.get('fetch_spot_patched') else "")
        ),
        f"**大盘水温**: {bench_line}",
        f"**候选分层**: L3股票{metrics['layer3']} -> Top15自选更新{len(watchlist_top15)} -> AI输入命中全量{len(selected_for_ai)}",
        f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        "",
        "**命中列表（AI分析输入，全量hits）代码 名称 | 来源标签 | 分值**",
        "",
    ]
    for code in selected_for_ai:
        name = name_map.get(code, code)
        reasons = "、".join(code_to_reasons.get(code, [])) or "L3共振通过"
        score = float(l3_score_map.get(code, 0.0))
        lines.append(f"• {code} {name} | {reasons} | score={score:.2f}")

    if not selected_for_ai:
        lines.append("无")
        lines.extend(
            [
                "",
                "**为什么没命中（L4触发=0）**",
                f"• Spring={int(by_trigger.get('spring', 0))}, "
                f"LPS={int(by_trigger.get('lps', 0))}, "
                f"EVR={int(by_trigger.get('evr', 0))}",
                "• 当前候选仅停留在 L3 共振阶段，尚未出现日线级别的 Spring/LPS/EVR 扳机信号。",
                "• AI 输入集合按 L4 命中构建，因此本轮为 0。",
            ]
        )

    if watchlist_top15:
        lines.extend(
            [
                "",
                "**Top15 自选更新（建议重点跟踪）代码 名称 | 行业 | 理由 | 分值**",
                "",
            ]
        )
        for row in watchlist_top15:
            code = str(row.get("code", ""))
            if not code:
                continue
            name = name_map.get(code, code)
            industry = str(row.get("industry", "")).strip() or "未知行业"
            reason = str(row.get("reason", "")).strip() or "L3共振通过"
            score = float(row.get("score", 0.0))
            lines.append(
                f"• {code} {name} | {industry} | {reason} | score={score:.2f}"
            )

    lines.extend(
        [
            "",
            f"**L3 全量清单（共{len(l3_ranked_rows)}）代码 名称 | 行业 | 选择原因 | 分值**",
            "",
        ]
    )
    if not l3_ranked_rows:
        lines.append("无")
    else:
        for row in l3_ranked_rows:
            code = str(row.get("code", ""))
            if not code:
                continue
            name = name_map.get(code, code)
            industry = str(row.get("industry", "")).strip() or "未知行业"
            reason = str(row.get("reason", "")).strip() or "L3共振通过"
            score = float(row.get("score", 0.0))
            lines.append(
                f"• {code} {name} | {industry} | {reason} | score={score:.2f}"
            )

    content = "\n".join(lines)
    title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
    ok = send_feishu_notification(webhook_url, title, content)

    symbols_for_report = [
        {
            "code": c,
            "name": name_map.get(c, c),
            "tag": "、".join(code_to_reasons.get(c, [])) or "L3共振通过",
        }
        for c in selected_for_ai
    ]
    return (ok, symbols_for_report, benchmark_context)
