from __future__ import annotations

import pandas as pd

from integrations.supabase_recommendation import (
    _build_us_performance_updates,
    _latest_market_records,
    refresh_us_tracking_performance,
)


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


def test_build_us_performance_updates_reprices_stale_initial_price():
    hist = pd.DataFrame(
        {
            "date": ["2026-05-15", "2026-05-18"],
            "high": [26.0, 31.0],
            "low": [24.0, 29.0],
            "close": [25.0, 30.0],
        }
    )
    grouped = {"SPLT.US": [{"id": 1, "code": "SPLT.US", "recommend_date": 20260515, "initial_price": 100.0}]}

    updates, _, _ = _build_us_performance_updates(grouped, {"SPLT.US": hist}, "now")

    assert updates[0]["initial_price"] == 25.0
    assert updates[0]["current_price"] == 30.0
    assert updates[0]["change_pct"] == 20.0
    assert updates[0]["mfe_pct"] == 24.0


def test_refresh_us_tracking_performance_fetches_forward_adjusted_hist(monkeypatch):
    captured: dict[str, object] = {}

    class FakeTickFlowClient:
        def __init__(self, api_key):
            assert api_key == "key"

        def get_klines_batch(self, symbols, *, period, count, adjust):
            captured["adjust"] = adjust
            assert symbols == ["SPLT.US"]
            assert period == "1d"
            assert count == 5
            return {"SPLT.US": pd.DataFrame({"date": ["2026-05-15"], "high": [26.0], "low": [24.0], "close": [25.0]})}

    monkeypatch.setenv("TICKFLOW_API_KEY", "key")
    monkeypatch.setattr("integrations.supabase_recommendation.is_supabase_configured", lambda: True)
    monkeypatch.setattr("integrations.supabase_recommendation._get_supabase_admin_client", lambda: object())
    monkeypatch.setattr(
        "integrations.supabase_recommendation._fetch_records_from_table",
        lambda *_args: [{"id": 1, "code": "SPLT.US", "recommend_date": 20260515, "initial_price": 100.0}],
    )
    monkeypatch.setattr("integrations.supabase_recommendation._upsert_to_table", lambda *_args: 1)
    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", FakeTickFlowClient)

    summary = refresh_us_tracking_performance(max_dates=1, kline_count=5)

    assert captured["adjust"] == "forward"
    assert summary["rows_updated"] == 1


def test_latest_market_records_keeps_latest_recommend_dates():
    rows = [
        {"code": "A.US", "recommend_date": 20260510},
        {"code": "B.US", "recommend_date": 20260512},
        {"code": "C.US", "recommend_date": 20260512},
        {"code": "D.US", "recommend_date": 20260513},
    ]

    assert _latest_market_records(rows, 2) == rows[1:]
