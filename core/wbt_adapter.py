"""Optional wbt integration for Wyckoff backtests.

The local ``wbt`` project is a weight-based Rust backtesting engine.  This
adapter keeps it as an optional analytics backend: Wyckoff still owns signal
generation, execution constraints, and trade replay; wbt can consume the
resulting NAV/weight data for a second, high-performance metric view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from importlib.util import find_spec
from typing import Any

import pandas as pd

SUPPORTED_WBT_ENGINES = {"legacy", "auto", "both", "wbt"}


@dataclass
class WbtEvaluation:
    """Container returned by the optional wbt evaluator."""

    requested: bool
    available: bool
    error: str = ""
    stats: dict[str, Any] | None = None
    long_stats: dict[str, Any] | None = None
    short_stats: dict[str, Any] | None = None
    daily_return: pd.DataFrame | None = None
    dailys: pd.DataFrame | None = None
    pairs: pd.DataFrame | None = None


def is_wbt_installed() -> bool:
    """Return True when the optional ``wbt`` Python package is importable."""

    return find_spec("wbt") is not None


def _record_get(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _next_trade_date(trade_dates: list[date], signal_date: date) -> date | None:
    for idx, day in enumerate(trade_dates):
        if day >= signal_date and idx + 1 < len(trade_dates):
            return trade_dates[idx + 1]
    return None


def _close_map_from_df(df: pd.DataFrame | None) -> dict[date, float]:
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return {}
    work = df[["date", "close"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["date", "close"]).sort_values("date")
    return {row.date: float(row.close) for row in work.itertuples(index=False)}


def build_position_weight_frame(
    *,
    records: list[Any],
    all_df_map: dict[str, pd.DataFrame],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    trade_dates: list[date],
    start_dt: date,
    end_dt: date,
) -> pd.DataFrame:
    """Convert replayed trades into a daily target-weight table.

    The output follows wbt's contract: ``dt, symbol, weight, price``.  It is an
    end-of-day target-weight approximation for audit/reporting.  Execution
    details such as T+1 entry, stop-loss, take-profit, and limit-up/limit-down
    constraints remain handled by ``scripts.backtest_runner`` before this step.
    """

    if not records:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])

    window_dates = [d for d in trade_dates if start_dt <= d <= end_dt]
    if not window_dates:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])

    positions: list[dict[str, Any]] = []
    for rec in records:
        code = str(_record_get(rec, "code", "") or "").strip()
        if not code:
            continue
        signal_date = _coerce_date(_record_get(rec, "signal_date"))
        exit_date = _coerce_date(_record_get(rec, "exit_date"))
        entry_date = _coerce_date(_record_get(rec, "entry_date"))
        if entry_date is None and signal_date is not None:
            entry_date = _next_trade_date(trade_dates, signal_date)
        if entry_date is None or exit_date is None or exit_date <= entry_date:
            continue
        positions.append({"code": code, "entry_date": entry_date, "exit_date": exit_date})

    if not positions:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])

    symbols = sorted({p["code"] for p in positions})
    pos_by_day: dict[date, list[dict[str, Any]]] = {}
    for day in window_dates:
        # wbt applies the weight at t to the return from t -> t+1.  Therefore a
        # position is weighted on entry_date <= t < exit_date, and the exit row
        # carries weight=0 while still providing the close price for liquidation.
        active = [p for p in positions if p["entry_date"] <= day < p["exit_date"]]
        if active:
            pos_by_day[day] = active

    close_maps: dict[str, dict[date, float]] = {}
    for code in symbols:
        from_cache = {d: float(v[3]) for d, v in (ohlc_cache.get(code) or {}).items() if v is not None and len(v) >= 4}
        close_maps[code] = from_cache or _close_map_from_df(all_df_map.get(code))

    rows: list[dict[str, Any]] = []
    for code in symbols:
        close_map = close_maps.get(code, {})
        last_price: float | None = None
        for day in window_dates:
            if day in close_map:
                last_price = close_map[day]
            if last_price is None or last_price <= 0:
                continue
            active = pos_by_day.get(day, [])
            n_active = len(active)
            if n_active:
                weight = sum(1.0 / n_active for p in active if p["code"] == code)
            else:
                weight = 0.0
            rows.append(
                {
                    "dt": pd.Timestamp(day),
                    "symbol": code,
                    "weight": float(weight),
                    "price": float(last_price),
                }
            )

    if not rows:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])
    return pd.DataFrame(rows).sort_values(["symbol", "dt"]).reset_index(drop=True)


def build_nav_weight_frame(nav_df: pd.DataFrame) -> pd.DataFrame:
    """Build a synthetic one-symbol wbt input from the legacy NAV curve.

    This lets wbt evaluate the already replayed Wyckoff portfolio without
    changing the strategy/execution semantics.  Costs are already embedded in
    the NAV curve, so callers should use ``fee_rate=0`` for this synthetic view.
    """

    if nav_df is None or nav_df.empty:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])
    work = nav_df[["date", "nav"]].copy()
    work["dt"] = pd.to_datetime(work["date"], errors="coerce")
    work["price"] = pd.to_numeric(work["nav"], errors="coerce")
    work = work.dropna(subset=["dt", "price"]).sort_values("dt")
    if work.empty:
        return pd.DataFrame(columns=["dt", "symbol", "weight", "price"])
    return pd.DataFrame(
        {
            "dt": work["dt"],
            "symbol": "WYCKOFF_PORTFOLIO",
            "weight": 1.0,
            "price": work["price"].astype(float),
        }
    ).reset_index(drop=True)


def evaluate_nav_with_wbt(
    nav_df: pd.DataFrame,
    *,
    fee_rate: float = 0.0,
    n_jobs: int = 1,
    yearly_days: int = 250,
) -> WbtEvaluation:
    """Evaluate a legacy NAV curve through the optional wbt backend."""

    if nav_df is None or nav_df.empty:
        return WbtEvaluation(requested=True, available=False, error="nav_df is empty")

    try:
        from wbt import WeightBacktest
    except Exception as exc:
        return WbtEvaluation(
            requested=True,
            available=False,
            error=f"wbt is not importable: {type(exc).__name__}: {exc}",
        )

    data = build_nav_weight_frame(nav_df)
    if data.empty:
        return WbtEvaluation(requested=True, available=False, error="wbt nav input is empty")

    try:
        wb = WeightBacktest(
            data,
            digits=6,
            fee_rate=float(fee_rate),
            n_jobs=max(int(n_jobs), 1),
            weight_type="ts",
            yearly_days=int(yearly_days),
        )
        return WbtEvaluation(
            requested=True,
            available=True,
            stats=dict(wb.stats),
            long_stats=dict(wb.long_stats),
            short_stats=dict(wb.short_stats),
            daily_return=wb.daily_return,
            dailys=wb.dailys,
            pairs=wb.pairs,
        )
    except Exception as exc:
        return WbtEvaluation(
            requested=True,
            available=False,
            error=f"wbt evaluation failed: {type(exc).__name__}: {exc}",
        )


def wbt_summary_fields(evaluation: WbtEvaluation) -> dict[str, Any]:
    """Flatten selected wbt stats for the existing markdown summary."""

    if not evaluation.requested:
        return {}
    out: dict[str, Any] = {
        "wbt_available": bool(evaluation.available),
        "wbt_error": evaluation.error,
    }
    stats = evaluation.stats or {}
    daily_mdd_pct = _daily_return_mdd_pct(evaluation.daily_return)
    ann_return_pct = _pct(stats.get("年化收益"))
    calmar_ratio = _float_or_none(stats.get("卡玛比率"))
    if ann_return_pct is not None and daily_mdd_pct is not None and daily_mdd_pct < 0:
        calmar_ratio = ann_return_pct / abs(daily_mdd_pct)
    if evaluation.available and stats:
        out.update(
            {
                "wbt_abs_return_pct": _pct(stats.get("绝对收益")),
                "wbt_ann_return_pct": ann_return_pct,
                "wbt_sharpe_ratio": _float_or_none(stats.get("夏普比率")),
                "wbt_calmar_ratio": calmar_ratio,
                "wbt_max_drawdown_pct": (
                    daily_mdd_pct if daily_mdd_pct is not None else _negative_pct(stats.get("最大回撤"))
                ),
                "wbt_daily_win_rate_pct": _pct(stats.get("日胜率")),
                "wbt_trade_count": stats.get("交易次数"),
            }
        )
    return out


def _daily_return_mdd_pct(daily_return: pd.DataFrame | None) -> float | None:
    """Calculate drawdown from wbt daily returns with an initial NAV=1 anchor."""

    if daily_return is None or daily_return.empty or "total" not in daily_return.columns:
        return None
    ret = pd.to_numeric(daily_return["total"], errors="coerce").dropna()
    if ret.empty:
        return None
    nav = (1.0 + ret).cumprod()
    nav = pd.concat([pd.Series([1.0]), nav], ignore_index=True)
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    return float(drawdown.min() * 100.0)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _pct(value: Any) -> float | None:
    v = _float_or_none(value)
    return None if v is None else v * 100.0


def _negative_pct(value: Any) -> float | None:
    v = _pct(value)
    return None if v is None else -abs(v)
