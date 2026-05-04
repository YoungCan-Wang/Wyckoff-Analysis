"""Synthetic OHLCV data generators for harness tests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_ohlcv(
    n: int = 250,
    trend: str = "up",
    base: float = 10.0,
    volatility: float = 0.02,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV DataFrame.

    Parameters
    ----------
    n : number of trading days
    trend : "up", "down", or "flat"
    base : starting price
    volatility : daily price movement scale
    seed : random seed for reproducibility
    """
    rng = np.random.default_rng(seed)
    drift_map = {"up": 0.002, "down": -0.002, "flat": 0.0}
    drift = drift_map.get(trend, 0.0)

    dates = pd.bdate_range(end=pd.Timestamp("2024-05-31"), periods=n)
    closes = [base]
    for _ in range(n - 1):
        change = drift + volatility * rng.standard_normal()
        closes.append(closes[-1] * (1 + change))

    closes_arr = np.array(closes)
    opens = closes_arr * (1 + rng.uniform(-0.005, 0.005, n))
    highs = np.maximum(opens, closes_arr) * (1 + rng.uniform(0, 0.015, n))
    lows = np.minimum(opens, closes_arr) * (1 - rng.uniform(0, 0.015, n))
    volumes = rng.integers(50000, 500000, size=n).astype(float)

    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes_arr,
            "volume": volumes,
        }
    )
