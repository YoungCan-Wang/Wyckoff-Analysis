# -*- coding: utf-8 -*-
"""
沙里淘金筛选逻辑：供 AI 分析页与沙里淘金页共用。
输入为已标准化的 data_map（symbol -> DataFrame with date, open, high, low, close, volume, pct_chg）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd


def normalize_hist_from_fetch(df: pd.DataFrame) -> pd.DataFrame:
    """将 fetch_a_share_csv._fetch_hist 返回的 DataFrame 转为筛选器所需格式。"""
    col_map = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "涨跌幅": "pct_chg",
    }
    out = df.rename(columns=col_map)
    keep = [c for c in ["date", "open", "high", "low", "close", "volume", "pct_chg"] if c in out.columns]
    out = out[keep].copy()
    if "pct_chg" not in out.columns and "close" in out.columns:
        out["pct_chg"] = out["close"].astype(float).pct_change() * 100
    for col in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


@dataclass
class ResisterConfig:
    benchmark_code: str = "000001"
    lookback_window: int = 3
    benchmark_drop_threshold: float = -2.0
    relative_strength_threshold: float = 2.0


@dataclass
class JumperConfig:
    consolidation_window: int = 60
    box_range: float = 0.25
    squeeze_window: int = 5
    squeeze_amplitude: float = 0.05
    volume_dry_ratio: float = 0.6
    volume_long_window: int = 50


@dataclass
class AnomalyConfig:
    volume_spike_ratio: float = 2.5
    stall_pct_limit: float = 2.0
    panic_pct_floor: float = -3.0
    volume_window: int = 5


@dataclass
class FirstBoardConfig:
    exclude_st: bool = True
    exclude_new_days: int = 30
    min_market_cap: float = 200000.0
    max_market_cap: float = 10000000.0
    lookback_limit_days: int = 10
    breakout_window: int = 60


@dataclass
class ScreenerConfig:
    trading_days: int = 500
    resister: ResisterConfig = field(default_factory=ResisterConfig)
    jumper: JumperConfig = field(default_factory=JumperConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    first_board: FirstBoardConfig = field(default_factory=FirstBoardConfig)


def _calc_cumulative_pct(df: pd.DataFrame) -> float:
    changes = df["pct_chg"].dropna() / 100.0
    return float((changes + 1).prod() - 1)


def _resister_bench_cum(benchmark_df: pd.DataFrame | None, cfg: ResisterConfig) -> float | None:
    """计算基准累计涨跌幅，不满足条件返回 None。"""
    if benchmark_df is None or benchmark_df.empty:
        return None
    bench = benchmark_df.sort_values("date").tail(cfg.lookback_window)
    if len(bench) < cfg.lookback_window:
        return None
    bench_cum = _calc_cumulative_pct(bench)
    if bench_cum * 100 >= cfg.benchmark_drop_threshold:
        return None
    return bench_cum


def screen_one_resister(
    symbol: str, df: pd.DataFrame, bench_cum: float, cfg: ResisterConfig
) -> tuple[str, float] | None:
    """单只股票抗跌主力筛选。"""
    window = df.sort_values("date").tail(cfg.lookback_window)
    if len(window) < cfg.lookback_window:
        return None
    stock_cum = _calc_cumulative_pct(window)
    score = (stock_cum - bench_cum) * 100
    if stock_cum >= 0 or score >= cfg.relative_strength_threshold:
        return (symbol, score)
    return None


def screen_resisters(
    data_map: dict[str, pd.DataFrame],
    benchmark_df: pd.DataFrame | None,
    cfg: ResisterConfig,
) -> list[tuple[str, float]]:
    if benchmark_df is None or benchmark_df.empty:
        return []
    bench = benchmark_df.sort_values("date").tail(cfg.lookback_window)
    if len(bench) < cfg.lookback_window:
        return []
    bench_cum = _calc_cumulative_pct(bench)
    if bench_cum * 100 >= cfg.benchmark_drop_threshold:
        return []
    results: list[tuple[str, float]] = []
    for symbol, df in data_map.items():
        window = df.sort_values("date").tail(cfg.lookback_window)
        if len(window) < cfg.lookback_window:
            continue
        stock_cum = _calc_cumulative_pct(window)
        score = (stock_cum - bench_cum) * 100
        if stock_cum >= 0 or score >= cfg.relative_strength_threshold:
            results.append((symbol, score))
    return results


def screen_one_anomaly(symbol: str, df: pd.DataFrame, cfg: AnomalyConfig) -> tuple[str, float] | None:
    """单只股票异常吸筹/出货筛选。"""
    df = df.sort_values("date")
    if len(df) < cfg.volume_window + 5:
        return None
    recent = df.iloc[-1]
    if recent["high"] <= recent["low"]:
        return None
    body = abs(recent["close"] - recent["open"])
    upper = recent["high"] - max(recent["open"], recent["close"])
    lower = min(recent["open"], recent["close"]) - recent["low"]
    vol_ma = df["volume"].rolling(window=cfg.volume_window).mean().iloc[-1]
    if vol_ma <= 0:
        return None
    vol_ratio = recent["volume"] / vol_ma
    pct_chg = float(recent["pct_chg"])
    high_stall = (
        vol_ratio >= cfg.volume_spike_ratio
        and pct_chg < cfg.stall_pct_limit
        and (upper >= 2 * body or recent["close"] < recent["open"])
    )
    low_support = (
        vol_ratio >= cfg.volume_spike_ratio
        and pct_chg > cfg.panic_pct_floor
        and lower >= 2 * body
    )
    if high_stall or low_support:
        return (symbol, float(vol_ratio))
    return None


def screen_anomalies(
    data_map: dict[str, pd.DataFrame], cfg: AnomalyConfig
) -> list[tuple[str, float]]:
    results: list[tuple[str, float]] = []
    for symbol, df in data_map.items():
        df = df.sort_values("date")
        if len(df) < cfg.volume_window + 5:
            continue
        recent = df.iloc[-1]
        if recent["high"] <= recent["low"]:
            continue
        body = abs(recent["close"] - recent["open"])
        range_val = recent["high"] - recent["low"]
        upper = recent["high"] - max(recent["open"], recent["close"])
        lower = min(recent["open"], recent["close"]) - recent["low"]
        vol_ma = df["volume"].rolling(window=cfg.volume_window).mean().iloc[-1]
        if vol_ma <= 0:
            continue
        vol_ratio = recent["volume"] / vol_ma
        pct_chg = float(recent["pct_chg"])

        high_stall = (
            vol_ratio >= cfg.volume_spike_ratio
            and pct_chg < cfg.stall_pct_limit
            and (upper >= 2 * body or recent["close"] < recent["open"])
        )
        low_support = (
            vol_ratio >= cfg.volume_spike_ratio
            and pct_chg > cfg.panic_pct_floor
            and lower >= 2 * body
        )
        if high_stall or low_support:
            score = float(vol_ratio)
            results.append((symbol, score))
    return results


def screen_one_jumper(
    symbol: str, df: pd.DataFrame, cfg: JumperConfig, stats: dict | None = None
) -> tuple[str, float] | None:
    """单只股票突破临界筛选。stats 为可选的统计累加 dict。"""
    window = max(cfg.consolidation_window, 20)
    df = df.sort_values("date")
    if stats is not None:
        stats["total"] = stats.get("total", 0) + 1
    if len(df) < window:
        return None
    recent = df.iloc[-window:]
    high = recent["high"].max()
    low = recent["low"].min()
    last_close = recent.iloc[-1]["close"]
    if last_close <= 0:
        return None
    box_range = (high - low) / last_close
    if box_range > cfg.box_range:
        return None
    if stats is not None:
        stats["box_pass"] = stats.get("box_pass", 0) + 1

    short = recent.tail(cfg.squeeze_window)
    short_high = short["high"].max()
    short_low = short["low"].min()
    short_close = short.iloc[-1]["close"]
    if short_close <= 0:
        return None
    short_amp = (short_high - short_low) / short_close
    if short_amp > cfg.squeeze_amplitude:
        return None
    if stats is not None:
        stats["squeeze_pass"] = stats.get("squeeze_pass", 0) + 1

    vol_short = short["volume"].mean()
    vol_long = recent["volume"].tail(cfg.volume_long_window).mean()
    if vol_long <= 0:
        return None
    if vol_short >= vol_long * cfg.volume_dry_ratio:
        return None
    if stats is not None:
        stats["volume_pass"] = stats.get("volume_pass", 0) + 1

    near_top = last_close >= low + (high - low) * 0.8
    near_bottom = last_close <= low + (high - low) * 0.2
    if near_top or near_bottom:
        if stats is not None:
            stats["position_pass"] = stats.get("position_pass", 0) + 1
        return (symbol, float(short_amp * 100))
    return None


def screen_jumpers(
    data_map: dict[str, pd.DataFrame],
    cfg: JumperConfig,
) -> tuple[list[tuple[str, float]], dict]:
    results: list[tuple[str, float]] = []
    stats = {
        "total": 0,
        "box_pass": 0,
        "squeeze_pass": 0,
        "volume_pass": 0,
        "position_pass": 0,
    }
    window = max(cfg.consolidation_window, 20)
    for symbol, df in data_map.items():
        stats["total"] += 1
        df = df.sort_values("date")
        if len(df) < window:
            continue
        recent = df.iloc[-window:]
        high = recent["high"].max()
        low = recent["low"].min()
        last_close = recent.iloc[-1]["close"]
        if last_close <= 0:
            continue
        box_range = (high - low) / last_close
        if box_range > cfg.box_range:
            continue
        stats["box_pass"] += 1

        short = recent.tail(cfg.squeeze_window)
        short_high = short["high"].max()
        short_low = short["low"].min()
        short_close = short.iloc[-1]["close"]
        if short_close <= 0:
            continue
        short_amp = (short_high - short_low) / short_close
        if short_amp > cfg.squeeze_amplitude:
            continue
        stats["squeeze_pass"] += 1

        vol_short = short["volume"].mean()
        vol_long = recent["volume"].tail(cfg.volume_long_window).mean()
        if vol_long <= 0:
            continue
        if vol_short >= vol_long * cfg.volume_dry_ratio:
            continue
        stats["volume_pass"] += 1

        near_top = last_close >= low + (high - low) * 0.8
        near_bottom = last_close <= low + (high - low) * 0.2
        if near_top or near_bottom:
            stats["position_pass"] += 1
            score = float(short_amp * 100)
            results.append((symbol, score))
    return results, stats


def screen_one_first_board(
    symbol: str,
    df: pd.DataFrame,
    cfg: FirstBoardConfig,
    *,
    stock_name_map: dict[str, str],
    list_date_fn: Callable[[str], date | None],
    market_cap_fn: Callable[[str], float],
) -> tuple[str, float] | None:
    """单只股票启动龙头筛选。"""
    df = df.sort_values("date")
    if len(df) < 2:
        return None
    if cfg.exclude_st:
        name = stock_name_map.get(symbol, "")
        if "ST" in name.upper():
            return None
    if cfg.exclude_new_days > 0:
        list_date = list_date_fn(symbol)
        if list_date is not None and (date.today() - list_date).days < cfg.exclude_new_days:
            return None
    last = df.iloc[-1]
    prev = df.iloc[-2]
    limit = 20.0 if symbol.startswith(("300", "301", "688")) else 10.0
    threshold = limit * 0.98
    curr_pct = float(last["pct_chg"])
    last_prev_pct = float(prev["pct_chg"])
    is_limit_up = curr_pct >= threshold
    prev_limit = last_prev_pct >= threshold
    if not is_limit_up or prev_limit:
        return None
    recent_limits = df.tail(cfg.lookback_limit_days).copy()
    recent_limits["pct_chg"] = pd.to_numeric(recent_limits["pct_chg"], errors="coerce")
    if (recent_limits["pct_chg"] >= threshold).sum() > 1:
        return None
    breakout_window = df.tail(cfg.breakout_window)
    if last["close"] < breakout_window["close"].max():
        return None
    if abs(last["high"] - last["low"]) < 1e-6:
        return None
    if cfg.min_market_cap > 0 or cfg.max_market_cap > 0:
        cap = market_cap_fn(symbol)
        if cap:
            if cfg.min_market_cap > 0 and cap < cfg.min_market_cap:
                return None
            if cfg.max_market_cap > 0 and cap > cfg.max_market_cap:
                return None
    return (symbol, float(last["pct_chg"]))


def screen_first_board(
    data_map: dict[str, pd.DataFrame],
    cfg: FirstBoardConfig,
    *,
    stock_name_map: dict[str, str],
    list_date_fn: Callable[[str], date | None],
    market_cap_fn: Callable[[str], float],
) -> list[tuple[str, float]]:
    results: list[tuple[str, float]] = []
    for symbol, df in data_map.items():
        df = df.sort_values("date")
        if len(df) < 2:
            continue
        if cfg.exclude_st:
            name = stock_name_map.get(symbol, "")
            if "ST" in name.upper():
                continue
        if cfg.exclude_new_days > 0:
            list_date = list_date_fn(symbol)
            if list_date is not None:
                if (date.today() - list_date).days < cfg.exclude_new_days:
                    continue
        last = df.iloc[-1]
        prev = df.iloc[-2]
        limit = 20.0 if symbol.startswith(("300", "301", "688")) else 10.0
        threshold = limit * 0.98

        curr_pct = float(last["pct_chg"])
        last_prev_pct = float(prev["pct_chg"])

        is_limit_up = curr_pct >= threshold
        prev_limit = last_prev_pct >= threshold
        if not is_limit_up or prev_limit:
            continue

        recent_limits = df.tail(cfg.lookback_limit_days).copy()
        recent_limits["pct_chg"] = pd.to_numeric(recent_limits["pct_chg"], errors="coerce")
        if (recent_limits["pct_chg"] >= threshold).sum() > 1:
            continue
        breakout_window = df.tail(cfg.breakout_window)
        if last["close"] < breakout_window["close"].max():
            continue
        if abs(last["high"] - last["low"]) < 1e-6:
            continue
        if cfg.min_market_cap > 0 or cfg.max_market_cap > 0:
            cap = market_cap_fn(symbol)
            if cap:
                if cfg.min_market_cap > 0 and cap < cfg.min_market_cap:
                    continue
                if cfg.max_market_cap > 0 and cap > cfg.max_market_cap:
                    continue
        score = float(last["pct_chg"])
        results.append((symbol, score))
    return results


def run_screener(
    tactic: str,
    data_map: dict[str, pd.DataFrame],
    benchmark_df: pd.DataFrame | None,
    config: ScreenerConfig,
    *,
    stock_name_map: dict[str, str] | None = None,
    list_date_fn: Callable[[str], date | None] | None = None,
    market_cap_fn: Callable[[str], float] | None = None,
) -> list[tuple[str, float]]:
    """执行一种战术筛选，返回 (symbol, score) 列表。"""
    if tactic == "抗跌主力":
        return screen_resisters(data_map, benchmark_df, config.resister)
    if tactic == "突破临界":
        results, _ = screen_jumpers(data_map, config.jumper)
        return results
    if tactic == "异常吸筹/出货":
        return screen_anomalies(data_map, config.anomaly)
    if tactic == "启动龙头":
        name_map = stock_name_map or {}
        list_fn = list_date_fn or (lambda _: None)
        cap_fn = market_cap_fn or (lambda _: 0.0)
        return screen_first_board(
            data_map, config.first_board,
            stock_name_map=name_map,
            list_date_fn=list_fn,
            market_cap_fn=cap_fn,
        )
    return []
