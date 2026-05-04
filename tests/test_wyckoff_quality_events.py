from __future__ import annotations

import pandas as pd

from core.kline_quality import check_kline_quality, check_kline_quality_map, summarize_quality_reports
from core.signal_lifecycle import evaluate_signal_lifecycle
from core.wyckoff_events import classify_wyckoff_event


def test_classify_wyckoff_event_right_side_ignition():
    event = classify_wyckoff_event(
        ("sos",),
        stage="Markup",
        channel="点火破局+结构TR",
        score=12.5,
        regime="RISK_ON",
    )

    assert event.event_id == "right_side_ignition"
    assert event.label == "右侧点火"
    assert event.track == "Trend"
    assert event.confidence == "high"


def test_kline_quality_detects_bad_ohlc_and_duplicate_date():
    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-01", "2024-01-03"],
            "open": [10, 11, 12],
            "high": [10.5, 10.8, 13],
            "low": [9.8, 11.2, 11],
            "close": [10.2, 10.7, 12.5],
            "volume": [1000, -1, 1200],
        }
    )

    report = check_kline_quality(df, symbol="000001")

    categories = {issue.category for issue in report.issues}
    assert not report.ok
    assert "duplicate_date" in categories
    assert "ohlc_inconsistent" in categories
    assert "negative_volume" in categories


def test_kline_quality_summary_counts_symbols():
    good = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02"],
            "open": [10, 10.2],
            "high": [10.5, 10.6],
            "low": [9.8, 10.0],
            "close": [10.2, 10.4],
            "volume": [1000, 1100],
        }
    )
    bad = good.assign(volume=[100, -1])

    summary = summarize_quality_reports(check_kline_quality_map({"000001": good, "000002": bad}))

    assert summary["total"] == 2
    assert summary["error_symbols"] == 1
    assert summary["ok"] == 1


def test_signal_lifecycle_marks_done_and_pending_horizons():
    df = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=6).astype(str),
            "close": [10, 11, 12, 11, 13, 14],
        }
    )

    lifecycle = evaluate_signal_lifecycle(df, code="000001", signal_date="2024-01-02", horizons=(1, 3, 10))

    assert lifecycle.code == "000001"
    assert lifecycle.entry_price == 11
    assert lifecycle.outcomes[0].status == "done"
    assert round(lifecycle.outcomes[0].return_pct, 2) == 9.09
    assert lifecycle.outcomes[-1].status == "pending"


def test_signal_lifecycle_uses_future_low_for_drawdown():
    df = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=3).astype(str),
            "close": [10, 11, 12],
            "low": [7, 8.5, 10.5],
        }
    )

    lifecycle = evaluate_signal_lifecycle(df, code="000001", signal_date="2024-01-01", horizons=(1,))

    assert round(lifecycle.outcomes[0].return_pct, 2) == 10.0
    assert round(lifecycle.outcomes[0].max_drawdown_pct, 2) == -15.0
