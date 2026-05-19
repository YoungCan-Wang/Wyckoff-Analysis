#!/usr/bin/env python3
"""Replay US backtest trades with explicit entry and partial-exit rules."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    description: str
    entry_rule: str
    pullback_pct: float
    target_multipliers: tuple[float, float]
    fallback_rule: str
    max_days: int


@dataclass(frozen=True)
class ReplayTrade:
    signal_date: str
    entry_date: str
    exit_date: str
    code: str
    name: str
    buy_price: float
    exit_value: float
    ret_pct: float
    trigger: str
    score: float


STRATEGIES = (
    StrategySpec(
        "s1_open_2x3x", "策略1", "开盘买入，2x/3x各卖50%，未成交按现价", "open", 0.0, (2.0, 3.0), "mark_to_market", 20
    ),
    StrategySpec(
        "s2_pullback30_2x3x",
        "策略2",
        "回撤30%买入，2x/3x各卖50%；满3日未成交按原价，不满3日按现价",
        "pullback",
        30.0,
        (2.0, 3.0),
        "original_or_mark",
        3,
    ),
    StrategySpec(
        "s3_pullback10_12x15x",
        "策略3",
        "回撤10%买入，1.2x/1.5x各卖50%；满3日剩余按最后一日1.2x开盘价，不满3日按现价",
        "pullback",
        10.0,
        (1.2, 1.5),
        "last_day_1_2x_open_or_mark",
        3,
    ),
    StrategySpec(
        "patch_a_open_12x15x",
        "补充A",
        "开盘买入，1.2x/1.5x各卖50%，未成交按3日后开盘价",
        "open",
        0.0,
        (1.2, 1.5),
        "open_after_3d",
        3,
    ),
    StrategySpec(
        "patch_b_pullback10_11x13x",
        "补充B",
        "回撤10%买入，1.1x/1.3x各卖50%，未成交按3日后开盘价",
        "pullback",
        10.0,
        (1.1, 1.3),
        "open_after_3d",
        3,
    ),
    StrategySpec(
        "patch_c_pullback20_12x15x",
        "补充C",
        "回撤20%买入，1.2x/1.5x各卖50%，未成交按3日后开盘价",
        "pullback",
        20.0,
        (1.2, 1.5),
        "open_after_3d",
        3,
    ),
)


def _parse_date(value: Any) -> date | None:
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(val) or math.isinf(val) else val


def _load_hist_map(snapshot_dir: Path) -> dict[str, pd.DataFrame]:
    hist_path = snapshot_dir / "hist_full.csv.gz"
    df = pd.read_csv(hist_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date", "symbol", "open", "high", "low", "close"])
    return {str(sym): g.sort_values("date").reset_index(drop=True) for sym, g in df.groupby("symbol")}


def _find_idx(candles: pd.DataFrame, target: date) -> int | None:
    dates = candles["date"].tolist()
    for idx, day in enumerate(dates):
        if day >= target:
            return idx
    return None


def _entry(strategy: StrategySpec, candles: pd.DataFrame, idx: int, base_price: float) -> tuple[int, float] | None:
    if strategy.entry_rule == "open":
        return idx, _safe_float(candles.iloc[idx]["open"], base_price)
    target = base_price * (1.0 - strategy.pullback_pct / 100.0)
    last_idx = min(idx + strategy.max_days, len(candles) - 1)
    for pos in range(idx, last_idx + 1):
        row = candles.iloc[pos]
        if _safe_float(row["low"], math.inf) <= target:
            open_px = _safe_float(row["open"], target)
            return pos, min(open_px, target)
    if last_idx <= idx and len(candles) <= idx + 1:
        return None
    row = candles.iloc[last_idx]
    if strategy.fallback_rule == "original_or_mark" and last_idx >= idx + strategy.max_days:
        return last_idx, base_price
    if last_idx >= idx + strategy.max_days:
        return last_idx, _safe_float(row["open"], base_price)
    return last_idx, _safe_float(row["close"], base_price)


def _fallback_exit(strategy: StrategySpec, candles: pd.DataFrame, idx: int, buy_price: float) -> tuple[int, float]:
    row = candles.iloc[idx]
    if strategy.fallback_rule == "last_day_1_2x_open_or_mark":
        return idx, _safe_float(row["open"], buy_price) * 1.2
    if strategy.fallback_rule == "open_after_3d":
        return idx, _safe_float(row["open"], buy_price)
    return idx, _safe_float(row["close"], buy_price)


def _exit(strategy: StrategySpec, candles: pd.DataFrame, buy_idx: int, buy_price: float) -> tuple[int, float] | None:
    start_idx = buy_idx + 1
    if start_idx >= len(candles):
        return None
    end_idx = (
        len(candles) - 1
        if strategy.fallback_rule == "mark_to_market"
        else min(buy_idx + strategy.max_days, len(candles) - 1)
    )
    proceeds = 0.0
    remaining = 1.0
    latest_exit_idx = start_idx
    scan_idx = start_idx
    for multiple in strategy.target_multipliers:
        target = buy_price * multiple
        hit_idx = _target_hit_idx(candles, scan_idx, end_idx, target)
        if hit_idx is None:
            continue
        open_px = _safe_float(candles.iloc[hit_idx]["open"], target)
        proceeds += 0.5 * max(open_px, target)
        remaining -= 0.5
        latest_exit_idx = hit_idx
        scan_idx = hit_idx
    if remaining > 0:
        fallback_idx, fallback_px = _fallback_exit(strategy, candles, end_idx, buy_price)
        proceeds += remaining * fallback_px
        latest_exit_idx = max(latest_exit_idx, fallback_idx)
    return latest_exit_idx, proceeds


def _target_hit_idx(candles: pd.DataFrame, start_idx: int, end_idx: int, target: float) -> int | None:
    for pos in range(start_idx, end_idx + 1):
        if _safe_float(candles.iloc[pos]["high"], -math.inf) >= target:
            return pos
    return None


def _replay_one(row: dict[str, Any], hist_map: dict[str, pd.DataFrame], strategy: StrategySpec) -> ReplayTrade | None:
    code = str(row.get("code") or "").strip()
    entry_date = _parse_date(row.get("entry_date"))
    base_price = _safe_float(row.get("entry_close"))
    candles = hist_map.get(code)
    if candles is None or entry_date is None or base_price <= 0:
        return None
    idx = _find_idx(candles, entry_date)
    if idx is None:
        return None
    entry = _entry(strategy, candles, idx, base_price)
    if entry is None:
        return None
    buy_idx, buy_price = entry
    exit_result = _exit(strategy, candles, buy_idx, buy_price)
    if exit_result is None or buy_price <= 0:
        return None
    exit_idx, exit_value = exit_result
    return _trade_from_result(row, candles, buy_idx, exit_idx, buy_price, exit_value)


def _trade_from_result(
    row: dict[str, Any], candles: pd.DataFrame, buy_idx: int, exit_idx: int, buy_price: float, exit_value: float
) -> ReplayTrade:
    return ReplayTrade(
        signal_date=str(row.get("signal_date") or ""),
        entry_date=str(candles.iloc[buy_idx]["date"]),
        exit_date=str(candles.iloc[exit_idx]["date"]),
        code=str(row.get("code") or ""),
        name=str(row.get("name") or row.get("code") or ""),
        buy_price=round(buy_price, 4),
        exit_value=round(exit_value, 4),
        ret_pct=round((exit_value / buy_price - 1.0) * 100.0, 4),
        trigger=str(row.get("trigger") or ""),
        score=_safe_float(row.get("score")),
    )


def _max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    for ret in returns:
        nav *= 1.0 + ret / 100.0
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    return mdd * 100.0


def _summary(strategy: StrategySpec, trades: list[ReplayTrade], period: dict[str, str], top_n: str) -> dict[str, Any]:
    returns = [t.ret_pct for t in trades]
    std = stdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean(returns) / std if std > 0 else None
    total = (math.prod(1.0 + r / 100.0 for r in returns) - 1.0) * 100.0 if returns else None
    return {
        "source": "local_us_strategy_replay",
        "period_key": period["key"],
        "period_label": period["label"],
        "start": period["start"],
        "end": period["end"],
        "top_n": int(top_n),
        "board": "us",
        "execution_strategy": asdict(strategy) | {"sell_after_buy_only": True, "target_scan_start": "after_entry"},
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "strategy_desc": strategy.description,
        "trades": len(returns),
        "win_rate_pct": (sum(1 for r in returns if r > 0) / len(returns) * 100.0) if returns else None,
        "avg_ret_pct": mean(returns) if returns else None,
        "median_ret_pct": median(returns) if returns else None,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": _max_drawdown(returns),
        "portfolio_total_ret_pct": total,
    }


def _write_outputs(out_dir: Path, strategy: StrategySpec, summary: dict[str, Any], trades: list[ReplayTrade]) -> None:
    strategy_dir = out_dir / strategy.id
    strategy_dir.mkdir(parents=True, exist_ok=True)
    (strategy_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (strategy_dir / "trades.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ReplayTrade.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(t) for t in trades)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay US backtest trades with six explicit strategy rules.")
    parser.add_argument("--trades-csv", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--period-key", required=True)
    parser.add_argument("--period-label", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top-n", default="2")
    args = parser.parse_args()

    rows = pd.read_csv(args.trades_csv).to_dict("records")
    hist_map = _load_hist_map(Path(args.snapshot_dir))
    period = {"key": args.period_key, "label": args.period_label, "start": args.start, "end": args.end}
    for strategy in STRATEGIES:
        trades = [t for row in rows if (t := _replay_one(row, hist_map, strategy)) is not None]
        summary = _summary(strategy, trades, period, str(args.top_n))
        _write_outputs(Path(args.output_dir), strategy, summary, trades)
        print(f"[us-replay] {strategy.name}: trades={len(trades)}, sharpe={summary.get('sharpe_ratio')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
