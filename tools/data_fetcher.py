"""
并行 OHLCV 批量拉取工具。

供 wyckoff_funnel、step3_batch_report、step4_rebalancer 统一使用，包括：
- 单票重试（进程/线程两种模式）
- SIGALRM 硬超时（Unix 限定）
- 批次并行 + 超时终止
- Spot 实时行情补丁（参数化 env_prefix 支持 FUNNEL/STEP3/STEP4 配置隔离）
"""

from __future__ import annotations

import os
import socket
import time
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from datetime import date, datetime

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.data_source import fetch_stock_spot_snapshot
from utils.trading_clock import CN_TZ

# ── 环境变量配置 ──

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


def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_hist_from_fetch(df)


def _fetch_hist(symbol: str, window, adjust: str) -> pd.DataFrame:
    from integrations.fetch_a_share_csv import _fetch_hist as _fh

    df = _fh(symbol=symbol, window=window, adjust=adjust)
    return _normalize_hist(df)


def _run_with_timeout(sym: str, window, timeout_s: int) -> pd.DataFrame:
    """
    在 worker 进程内给单票请求加硬超时（Unix 下用 SIGALRM）。
    若平台不支持 SIGALRM（例如 Windows），则退化为直接调用。
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


def fetch_one_with_retry(sym: str, window, max_retries: int = MAX_RETRIES) -> tuple[str, pd.DataFrame | None]:
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


def fetch_one_with_retry_thread(sym: str, window, max_retries: int = MAX_RETRIES) -> tuple[str, pd.DataFrame | None]:
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


def latest_trade_date_from_hist(df: pd.DataFrame) -> date | None:
    """从 DataFrame 提取最新交易日。"""
    if df is None or df.empty or "date" not in df.columns:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    if s.empty:
        return None
    return s.iloc[-1].date()


def append_spot_bar_if_needed(
    code: str,
    df: pd.DataFrame,
    target_trade_date: date,
    *,
    env_prefix: str = "FUNNEL",
    sleep_default: float = 0.2,
    zero_fallback: bool = False,
) -> tuple[pd.DataFrame, bool]:
    """
    如果 DataFrame 最新交易日缺失且今天是目标交易日，
    通过实时行情补丁补齐最后一根 bar。

    Parameters
    ----------
    code : str
        股票代码
    env_prefix : str
        环境变量前缀，用于读取 {prefix}_ENABLE_SPOT_PATCH 等配置。
        默认 "FUNNEL"，step3 传 "STEP3"，step4 传 "STEP4"。
    sleep_default : float
        重试间隔默认值（秒）。
    zero_fallback : bool
        turnover_ok=False 时的回退策略：
        True  → volume/amount 置 0（避免污染均量/ATR 计算，step3/step4 行为）
        False → 沿用前一日 volume/amount 或 NaN（funnel 行为）
    """
    enable = os.getenv(f"{env_prefix}_ENABLE_SPOT_PATCH", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enable or df is None or df.empty:
        return (df, False)
    latest_trade = latest_trade_date_from_hist(df)
    if latest_trade is None or latest_trade >= target_trade_date:
        return (df, False)
    if target_trade_date != datetime.now(CN_TZ).date():
        return (df, False)

    retries = int(os.getenv(f"{env_prefix}_SPOT_PATCH_RETRIES", "2"))
    sleep_s = float(os.getenv(f"{env_prefix}_SPOT_PATCH_SLEEP", str(sleep_default)))

    df_s = df.sort_values("date").reset_index(drop=True)
    last_close_series = pd.to_numeric(df_s.get("close"), errors="coerce").dropna()
    prev_close = float(last_close_series.iloc[-1]) if not last_close_series.empty else None

    # 仅在非 zero_fallback 模式下提取前日量能作为回退值
    prev_volume = None
    prev_amount = None
    if not zero_fallback:
        if "volume" in df_s.columns:
            vol_s = pd.to_numeric(df_s.get("volume"), errors="coerce").dropna()
            if not vol_s.empty:
                prev_volume = float(vol_s.iloc[-1])
        if "amount" in df_s.columns:
            amt_s = pd.to_numeric(df_s.get("amount"), errors="coerce").dropna()
            if not amt_s.empty:
                prev_amount = float(amt_s.iloc[-1])

    for attempt in range(max(retries, 1)):
        snap = fetch_stock_spot_snapshot(code, force_refresh=attempt > 0)
        close_v = None if not snap else snap.get("close")
        if close_v is None or float(close_v) <= 0:
            if attempt < max(retries, 1) - 1:
                time.sleep(max(sleep_s, 0.0))
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
        elif zero_fallback:
            # 单位不确定时，宁可放弃量能，避免污染均量/ATR 相关计算。
            volume_f = 0.0
            amount_f = 0.0
        else:
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


def terminate_executor_processes(ex: ProcessPoolExecutor, batch_no: int) -> None:
    """
    批次超时时，主动终止仍存活的子进程，避免 wait=False 仅"逻辑结束"但进程继续跑。
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
        except Exception:
            pass
    if killed:
        print(f"[funnel] 批次#{batch_no} 已强制终止 {killed} 个卡住子进程")


def fetch_all_ohlcv(
    symbols: list[str],
    window,
    *,
    enforce_target_trade_date: bool = False,
    batch_size: int = BATCH_SIZE,
    max_workers: int = MAX_WORKERS,
    batch_timeout: int = BATCH_TIMEOUT,
    batch_sleep: float = BATCH_SLEEP,
    executor_mode: str = EXECUTOR_MODE,
) -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    """
    批量并行拉取 OHLCV 数据。

    返回:
      (df_map, fetch_stats)
      - df_map: symbol -> DataFrame
      - fetch_stats: {"fetch_ok", "fetch_fail", "fetch_date_mismatch",
                      "fetch_spot_patched", "fetch_elapsed_s", "fetch_qps"}
    """
    all_df_map: dict[str, pd.DataFrame] = {}
    fetch_ok = 0
    fetch_fail = 0
    fetch_date_mismatch = 0
    fetch_spot_patched = 0
    total_batches = (len(symbols) + batch_size - 1) // batch_size if symbols else 0

    print(
        f"[funnel] 开始拉取 {len(symbols)} 只股票日线 "
        f"(executor={executor_mode}, batch_size={batch_size}, max_workers={max_workers}, "
        f"batch_timeout={batch_timeout}s, fetch_timeout={FETCH_TIMEOUT}s, retries={MAX_RETRIES})"
    )
    from cli.progress import report_progress

    report_progress("拉取日线", f"共{len(symbols)}只, {total_batches}批", 0.0)
    total_fetch_started = time.monotonic()
    for i in range(0, len(symbols), batch_size):
        batch_no = i // batch_size + 1
        batch = symbols[i : i + batch_size]
        batch_ok = 0
        batch_fail = 0
        batch_started = time.monotonic()
        print(f"[funnel] 批次#{batch_no}/{total_batches} 启动，股票数={len(batch)}")

        use_process = executor_mode == "process"
        ex = (
            ProcessPoolExecutor(max_workers=max_workers) if use_process else ThreadPoolExecutor(max_workers=max_workers)
        )
        fetch_fn = fetch_one_with_retry if use_process else fetch_one_with_retry_thread
        futures = {ex.submit(fetch_fn, s, window): s for s in batch}
        try:
            for f in as_completed(futures, timeout=batch_timeout):
                sym = futures[f]
                try:
                    _, df = f.result()
                except Exception as e:
                    print(f"[funnel] 批次#{batch_no} 拉取失败 {sym}: {e}")
                    batch_fail += 1
                    fetch_fail += 1
                    continue
                if df is not None:
                    if enforce_target_trade_date:
                        ltd = latest_trade_date_from_hist(df)
                        if ltd != window.end_trade_date:
                            df, patched = append_spot_bar_if_needed(
                                sym,
                                df,
                                window.end_trade_date,
                            )
                            if patched:
                                ltd = latest_trade_date_from_hist(df)
                                fetch_spot_patched += 1
                            batch_fail += 1
                            if ltd != window.end_trade_date:
                                fetch_fail += 1
                                fetch_date_mismatch += 1
                                print(
                                    f"[funnel] 批次#{batch_no} 跳过 {sym}: "
                                    f"latest_trade_date={ltd}, "
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
                f"[funnel] 批次#{batch_no} 超时({batch_timeout}s)，"
                f"已完成={batch_ok + batch_fail - timed_out}/{len(batch)}，"
                f"未完成={timed_out}，将跳过剩余任务"
            )
            if pending_symbols:
                preview = ", ".join(pending_symbols[:10])
                suffix = "..." if len(pending_symbols) > 10 else ""
                print(f"[funnel] 批次#{batch_no} 超时股票: {preview}{suffix}")
            if use_process:
                terminate_executor_processes(ex, batch_no)
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
        report_progress("拉取日线", f"批次#{batch_no}/{total_batches}", batch_no / total_batches)
        if i + batch_size < len(symbols) and batch_sleep > 0:
            time.sleep(batch_sleep)

    total_fetch_elapsed = time.monotonic() - total_fetch_started
    overall_qps = (fetch_ok / total_fetch_elapsed) if total_fetch_elapsed > 0 else 0.0
    print(
        f"[funnel] 日线拉取完成: 成功={fetch_ok}, 失败={fetch_fail}, "
        f"总耗时={total_fetch_elapsed:.1f}s, 平均qps={overall_qps:.2f}"
    )
    report_progress("拉取完成", f"成功={fetch_ok}, 失败={fetch_fail}", 1.0)
    if enforce_target_trade_date:
        print(
            f"[funnel] 交易日对齐检查: mismatch={fetch_date_mismatch}, "
            f"spot_patched={fetch_spot_patched}, target_trade_date={window.end_trade_date}"
        )

    stats = {
        "fetch_ok": fetch_ok,
        "fetch_fail": fetch_fail,
        "fetch_date_mismatch": fetch_date_mismatch,
        "fetch_spot_patched": fetch_spot_patched,
        "fetch_elapsed_s": round(total_fetch_elapsed, 2),
        "fetch_qps": round(overall_qps, 3),
    }
    return (all_df_map, stats)
