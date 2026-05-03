from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from core.stock_cache import CacheMeta


def test_get_stock_hist_uses_cache_when_only_tail_non_trading_gap_fails(monkeypatch):
    import integrations.stock_hist_repository as repo

    cached = pd.DataFrame(
        [
            {
                "date": "2026-04-29",
                "open": 10.0,
                "high": 11.0,
                "low": 9.8,
                "close": 10.5,
                "volume": 1000,
                "amount": 10000,
                "pct_chg": 1.0,
            },
            {
                "date": "2026-04-30",
                "open": 10.5,
                "high": 11.2,
                "low": 10.1,
                "close": 11.0,
                "volume": 1200,
                "amount": 13000,
                "pct_chg": 4.76,
            },
        ]
    )

    monkeypatch.setattr(
        repo,
        "get_cache_meta",
        lambda *args, **kwargs: CacheMeta(
            symbol="000001",
            adjust="qfq",
            source="cache",
            start_date=date(2026, 4, 29),
            end_date=date(2026, 4, 30),
            updated_at=datetime(2026, 4, 30, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(repo, "load_cached_history", lambda *args, **kwargs: cached)

    def fail_tail_gap(*args, **kwargs):
        raise RuntimeError("数据拉取全线失败 [标:000001, 范围:20260501..20260501, 复权:qfq]")

    monkeypatch.setattr(repo, "_fetch_gap", fail_tail_gap)
    monkeypatch.setattr(repo, "upsert_cache_data", lambda *args, **kwargs: None)

    out = repo.get_stock_hist("000001", date(2026, 4, 29), date(2026, 5, 1))

    assert len(out) == 2
    assert out.iloc[-1]["日期"] == "2026-04-30"
    assert out.attrs["cache_status"] == "hit_tail_gap_skipped"
    assert out.attrs["cached_until"] == "2026-04-30"
