"""Signal lifecycle evaluation for Wyckoff events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class HorizonOutcome:
    horizon: int
    status: str
    return_pct: float | None
    max_drawdown_pct: float | None


@dataclass(frozen=True)
class SignalLifecycle:
    code: str
    signal_date: str
    entry_price: float | None
    outcomes: tuple[HorizonOutcome, ...]


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def evaluate_signal_lifecycle(
    df: pd.DataFrame,
    *,
    code: str = "",
    signal_date: str | None = None,
    entry_price: float | None = None,
    horizons: Iterable[int] = (1, 3, 5, 10),
) -> SignalLifecycle:
    """Evaluate forward returns after a signal date.

    If future bars are not available yet, the corresponding horizon is marked
    as ``pending``.  This makes the function usable both in historical replay
    and in today's live run.
    """

    if df is None or df.empty or "close" not in df.columns:
        return SignalLifecycle(code=str(code or ""), signal_date=str(signal_date or ""), entry_price=None, outcomes=())

    work = df.copy()
    if "date" in work.columns:
        work["_dt"] = pd.to_datetime(work["date"], errors="coerce")
        work = work.sort_values("_dt").reset_index(drop=True)
    else:
        work = work.reset_index(drop=False).rename(columns={"index": "_dt"})
    close = _to_numeric(work["close"]).reset_index(drop=True)
    if close.dropna().empty:
        return SignalLifecycle(code=str(code or ""), signal_date=str(signal_date or ""), entry_price=None, outcomes=())
    low = _to_numeric(work["low"]).reset_index(drop=True) if "low" in work.columns else close

    if signal_date and "date" in work.columns:
        target_dt = pd.to_datetime(signal_date, errors="coerce")
        idx_matches = work.index[pd.to_datetime(work["date"], errors="coerce") == target_dt].tolist()
        signal_pos = int(idx_matches[-1]) if idx_matches else int(len(work) - 1)
    else:
        signal_pos = int(len(work) - 1)

    signal_pos = min(max(signal_pos, 0), len(work) - 1)
    signal_close = close.iloc[signal_pos]
    base_price = float(entry_price) if entry_price is not None else float(signal_close)
    if base_price <= 0 or pd.isna(base_price):
        base_price = None

    if "date" in work.columns:
        dt_value = work["date"].iloc[signal_pos]
        signal_date_s = str(pd.to_datetime(dt_value, errors="coerce").date())
    else:
        signal_date_s = str(signal_pos)

    outcomes: list[HorizonOutcome] = []
    for horizon_raw in horizons:
        horizon = max(int(horizon_raw), 1)
        end_pos = signal_pos + horizon
        if base_price is None or end_pos >= len(close):
            outcomes.append(HorizonOutcome(horizon=horizon, status="pending", return_pct=None, max_drawdown_pct=None))
            continue
        future_close = close.iloc[end_pos]
        if pd.isna(future_close):
            outcomes.append(HorizonOutcome(horizon=horizon, status="invalid", return_pct=None, max_drawdown_pct=None))
            continue
        path = low.iloc[signal_pos + 1 : end_pos + 1].dropna()
        min_path_price = min(base_price, float(path.min()) if not path.empty else float(future_close))
        ret = (float(future_close) - base_price) / base_price * 100.0
        mdd = (min_path_price - base_price) / base_price * 100.0
        outcomes.append(
            HorizonOutcome(
                horizon=horizon,
                status="done",
                return_pct=float(ret),
                max_drawdown_pct=float(mdd),
            )
        )

    return SignalLifecycle(
        code=str(code or ""),
        signal_date=signal_date_s,
        entry_price=base_price,
        outcomes=tuple(outcomes),
    )


__all__ = ["HorizonOutcome", "SignalLifecycle", "evaluate_signal_lifecycle"]
