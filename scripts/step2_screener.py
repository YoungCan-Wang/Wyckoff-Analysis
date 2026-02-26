# -*- coding: utf-8 -*-
"""
阶段 2：沙里淘金（主板+创业板）
四种战术筛选 → 飞书发送股票代码

环境变量：STEP2_BATCH_SIZE, STEP2_BATCH_SLEEP, STEP2_MAX_WORKERS, STEP2_LIMIT_STOCKS,
         STEP2_FIRST_BOARD_LITE(默认 true)：定时任务下关闭上市日期/市值过滤，避免大量 stock_individual_info_em 请求
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
import pandas as pd

from fetch_a_share_csv import (
    _resolve_trading_window,
    get_stocks_by_board,
    _normalize_symbols,
)
from wyckoff_engine import (
    ScreenerConfig,
    FirstBoardConfig,
    _resister_bench_cum,
    screen_one_resister,
    screen_one_anomaly,
    screen_one_jumper,
    screen_one_first_board,
)
from utils.feishu import send_feishu_notification

LABELS = {
    "resisters": "抗跌主力",
    "jumpers": "突破临界",
    "anomalies": "异常吸筹/出货",
    "first_board": "启动龙头",
}
TOP_N = 10
TRADING_DAYS = 500
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2


def _config() -> dict:
    return {
        "batch_size": int(os.getenv("STEP2_BATCH_SIZE", "200")),
        "batch_sleep": int(os.getenv("STEP2_BATCH_SLEEP", "5")),
        "max_workers": int(os.getenv("STEP2_MAX_WORKERS", "8")),
        "limit_stocks": int(os.getenv("STEP2_LIMIT_STOCKS", "800")),
        "first_board_lite": os.getenv("STEP2_FIRST_BOARD_LITE", "true").lower() in ("1", "true", "yes"),
    }


def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "涨跌幅": "pct_chg",
    }
    out = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    keep = [c for c in ["date", "open", "high", "low", "close", "volume", "pct_chg"] if c in out.columns]
    out = out[keep].copy()
    for col in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _fetch_hist(symbol: str, window, adjust: str) -> pd.DataFrame:
    from fetch_a_share_csv import _fetch_hist as _fh
    df = _fh(symbol=symbol, window=window, adjust=adjust)
    return _normalize_hist(df)


def _fetch_index(code: str, start: str, end: str) -> pd.DataFrame:
    df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
    return _normalize_hist(df)


def _stock_name_map() -> dict[str, str]:
    try:
        items = ak.stock_info_a_code_name()
        return {str(r["code"]): str(r["name"]) for _, r in items.iterrows()}
    except Exception:
        return {}


def _list_date(symbol: str) -> date | None:
    try:
        df = ak.stock_individual_info_em(symbol=symbol)
        if df is None or df.empty:
            return None
        row = df.loc[df["item"] == "上市日期", "value"]
        if row.empty:
            return None
        from datetime import datetime
        return datetime.strptime(str(row.iloc[0]).strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _market_cap(symbol: str) -> float:
    try:
        df = ak.stock_individual_info_em(symbol=symbol)
        if df is None or df.empty:
            return 0.0
        row = df.loc[df["item"] == "总市值", "value"]
        if row.empty:
            return 0.0
        raw = str(row.iloc[0]).strip()
        if raw.endswith("亿"):
            return float(raw.replace("亿", "")) * 10000
        if raw.endswith("万"):
            return float(raw.replace("万", ""))
        return float(raw)
    except Exception:
        return 0.0


def _fetch_one_with_retry(sym: str, window, max_retries: int = MAX_RETRIES) -> tuple[str, pd.DataFrame | None]:
    for attempt in range(max_retries):
        try:
            df = _fetch_hist(sym, window, "qfq")
            return (sym, df)
        except Exception:
            if attempt < max_retries - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
    return (sym, None)


def run_screener_job() -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行沙里淘金，返回 (四类结果, 质量指标)。"""
    cfg = ScreenerConfig(trading_days=TRADING_DAYS)
    opts = _config()
    window = _resolve_trading_window(
        end_calendar_day=date.today() - timedelta(days=1),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    main_stocks = [s["code"] for s in get_stocks_by_board("main") if s.get("code")]
    chinext_stocks = [s["code"] for s in get_stocks_by_board("chinext") if s.get("code")]
    symbols = _normalize_symbols(main_stocks + chinext_stocks)[:opts["limit_stocks"]]

    bench_cum = None
    try:
        bench_df = _fetch_index(cfg.resister.benchmark_code, start_s, end_s)
        bench_cum = _resister_bench_cum(bench_df, cfg.resister)
    except Exception:
        pass

    results: dict[str, list[tuple[str, float]]] = {
        "resisters": [], "jumpers": [], "anomalies": [], "first_board": [],
    }
    jump_stats: dict[str, int] = {"total": 0, "box_pass": 0, "squeeze_pass": 0, "volume_pass": 0, "position_pass": 0}
    name_map = _stock_name_map()
    fetch_ok = 0
    fetch_fail = 0

    # 定时任务下使用 lite 配置，关闭上市日期/市值过滤，避免大量 stock_individual_info_em 请求
    first_board_lite = opts.get("first_board_lite", True)
    if first_board_lite:
        print("[step2] STEP2_FIRST_BOARD_LITE=true，启动龙头跳过上市日期/市值过滤")
    first_board_cfg = cfg.first_board
    list_fn = _list_date
    cap_fn = _market_cap
    if first_board_lite:
        first_board_cfg = FirstBoardConfig(
            exclude_st=cfg.first_board.exclude_st,
            exclude_new_days=0,  # 不按上市日期过滤
            min_market_cap=0.0,
            max_market_cap=0.0,  # 不按市值过滤
            lookback_limit_days=cfg.first_board.lookback_limit_days,
            breakout_window=cfg.first_board.breakout_window,
        )
        list_fn = lambda _: None  # noqa: E731
        cap_fn = lambda _: 0.0  # noqa: E731

    def _screen(sym: str, df: pd.DataFrame) -> None:
        if bench_cum is not None:
            hit = screen_one_resister(sym, df, bench_cum, cfg.resister)
            if hit:
                results["resisters"].append(hit)
        hit = screen_one_jumper(sym, df, cfg.jumper, jump_stats)
        if hit:
            results["jumpers"].append(hit)
        hit = screen_one_anomaly(sym, df, cfg.anomaly)
        if hit:
            results["anomalies"].append(hit)
        hit = screen_one_first_board(
            sym, df, first_board_cfg,
            stock_name_map=name_map,
            list_date_fn=list_fn,
            market_cap_fn=cap_fn,
        )
        if hit:
            results["first_board"].append(hit)

    batch_size = opts["batch_size"]
    batch_sleep = opts["batch_sleep"]
    max_workers = opts["max_workers"]

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fetch_one_with_retry, s, window): s for s in batch}
            for f in as_completed(futures):
                sym, df = f.result()
                if df is not None:
                    fetch_ok += 1
                    _screen(sym, df)
                else:
                    fetch_fail += 1
        if i + batch_size < len(symbols) and batch_sleep > 0:
            time.sleep(batch_sleep)

    total_hits = sum(len(v) for v in results.values())
    metrics = {
        "total_symbols": len(symbols),
        "fetch_ok": fetch_ok,
        "fetch_fail": fetch_fail,
        "total_hits": total_hits,
        "by_tactic": {k: len(v) for k, v in results.items()},
    }
    print(f"[step2] 总股票={metrics['total_symbols']}, 成功={fetch_ok}, 失败={fetch_fail}, 命中={total_hits}, 各战术={metrics['by_tactic']}")
    return results, metrics


def run(webhook_url: str) -> tuple[bool, list[str]]:
    """执行沙里淘金、发送飞书，返回 (成功与否, 用于研报的股票代码列表)。"""
    results, _ = run_screener_job()
    lines = [f"**{LABELS[k]}** (Top {TOP_N})"]
    seen: set[str] = set()
    symbols_for_report: list[str] = []
    for k, label in LABELS.items():
        pairs = sorted(results.get(k, []), key=lambda x: -x[1])[:TOP_N]
        codes = " ".join(c[0] for c in pairs) if pairs else "无"
        lines.append(f"{label}: {codes}")
        for c, _ in pairs:
            if c not in seen:
                seen.add(c)
                symbols_for_report.append(c)
    content = "\n".join(lines)
    title = f"🧭 沙里淘金 {date.today().strftime('%Y-%m-%d')}"
    ok = send_feishu_notification(webhook_url, title, content)
    max_for_report = int(os.getenv("STEP3_MAX_SYMBOLS", "6"))
    return (ok, symbols_for_report[:max_for_report])
