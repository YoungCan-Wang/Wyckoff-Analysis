from __future__ import annotations

import pandas as pd

from integrations.supabase_recommendation import _build_us_performance_updates, _latest_market_records


def test_build_us_performance_updates_uses_entry_trade_date_window():
    hist = pd.DataFrame(
        {
            "date": ["2026-05-15", "2026-05-18"],
            "high": [10.8, 12.0],
            "low": [9.8, 9.0],
            "close": [10.0, 11.0],
        }
    )
    grouped = {"ABC.US": [{"id": 1, "code": "ABC.US", "recommend_date": 20260516, "initial_price": 10.0}]}

    updates, codes_no_data, latest_td = _build_us_performance_updates(grouped, {"ABC.US": hist}, "now")

    assert codes_no_data == 0
    assert latest_td == "20260518"
    assert updates == [
        {
            "id": 1,
            "code": "ABC.US",
            "recommend_date": 20260516,
            "initial_price": 10.0,
            "current_price": 11.0,
            "change_pct": 10.0,
            "mfe_pct": 20.0,
            "mae_pct": -10.0,
            "range_amp_pct": 33.33,
            "mfe_price": 12.0,
            "mae_price": 9.0,
            "mfe_date": 20260518,
            "mae_date": 20260518,
            "performance_days": 2,
            "performance_updated_at": "now",
            "updated_at": "now",
        }
    ]


def test_latest_market_records_keeps_latest_recommend_dates():
    rows = [
        {"code": "A.US", "recommend_date": 20260510},
        {"code": "B.US", "recommend_date": 20260512},
        {"code": "C.US", "recommend_date": 20260512},
        {"code": "D.US", "recommend_date": 20260513},
    ]

    assert _latest_market_records(rows, 2) == rows[1:]
