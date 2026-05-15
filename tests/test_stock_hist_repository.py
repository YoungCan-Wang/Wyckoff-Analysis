from __future__ import annotations

from datetime import date

import pandas as pd


def test_get_stock_hist_returns_data_from_source(monkeypatch):
    import integrations.stock_hist_repository as repo

    fake_df = pd.DataFrame(
        [
            {
                "日期": "2026-04-29",
                "开盘": 10.0,
                "最高": 11.0,
                "最低": 9.8,
                "收盘": 10.5,
                "成交量": 1000,
                "成交额": 10000,
                "涨跌幅": 1.0,
            },
            {
                "日期": "2026-04-30",
                "开盘": 10.5,
                "最高": 11.2,
                "最低": 10.1,
                "收盘": 11.0,
                "成交量": 1200,
                "成交额": 13000,
                "涨跌幅": 4.76,
            },
        ]
    )
    fake_df.attrs["source"] = "tickflow"

    monkeypatch.setattr(repo, "fetch_stock_hist_from_source", lambda **kwargs: fake_df)

    out = repo.get_stock_hist("000001", date(2026, 4, 29), date(2026, 4, 30))

    assert len(out) == 2
    assert out.iloc[-1]["日期"] == "2026-04-30"
    assert out.attrs["source"] == "realtime"
    assert out.attrs["upstream_source"] == "tickflow"


def test_get_stock_hist_accepts_legacy_kwargs(monkeypatch):
    """旧调用方可能传 context/cache_only/user_id，确认不报错。"""
    import integrations.stock_hist_repository as repo

    fake_df = pd.DataFrame(
        [
            {
                "日期": "2026-04-29",
                "开盘": 10.0,
                "最高": 11.0,
                "最低": 9.8,
                "收盘": 10.5,
                "成交量": 1000,
                "成交额": 10000,
                "涨跌幅": 1.0,
            }
        ]
    )
    fake_df.attrs["source"] = "test"
    monkeypatch.setattr(repo, "fetch_stock_hist_from_source", lambda **kwargs: fake_df)

    out = repo.get_stock_hist("000001", "2026-04-29", "2026-04-29", context="background", cache_only=True, user_id="u1")
    assert len(out) == 1
