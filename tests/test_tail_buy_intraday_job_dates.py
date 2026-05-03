from __future__ import annotations

from datetime import date, datetime

from integrations.fetch_a_share_csv import TradingWindow
from scripts import tail_buy_intraday_job as tail_job


def test_resolve_trade_dates_on_trading_day_uses_prev_trade_and_today(monkeypatch):
    monkeypatch.setattr(
        tail_job,
        "_now",
        lambda: datetime(2026, 4, 27, 14, 10, tzinfo=tail_job.TZ),  # 周一交易时段
    )

    def fake_window(*, end_calendar_day: date, trading_days: int) -> TradingWindow:
        assert end_calendar_day == date(2026, 4, 27)
        assert trading_days == 2
        return TradingWindow(
            start_trade_date=date(2026, 4, 24),  # 上周五
            end_trade_date=date(2026, 4, 27),  # 周一
        )

    monkeypatch.setattr(tail_job, "_resolve_trading_window", fake_window)
    prev_trade, today_trade = tail_job._resolve_trade_dates()
    assert prev_trade == "2026-04-24"
    assert today_trade == "2026-04-27"


def test_resolve_trade_dates_on_non_trading_day_targets_latest_trade(monkeypatch):
    monkeypatch.setattr(
        tail_job,
        "_now",
        lambda: datetime(2026, 4, 26, 10, 0, tzinfo=tail_job.TZ),  # 周日
    )

    def fake_window(*, end_calendar_day: date, trading_days: int) -> TradingWindow:
        assert end_calendar_day == date(2026, 4, 26)
        assert trading_days == 2
        return TradingWindow(
            start_trade_date=date(2026, 4, 23),
            end_trade_date=date(2026, 4, 24),  # 最新交易日（周五）
        )

    monkeypatch.setattr(tail_job, "_resolve_trading_window", fake_window)
    prev_trade, today_trade = tail_job._resolve_trade_dates()
    assert prev_trade == "2026-04-24"
    assert today_trade == "2026-04-24"


def test_resolve_trade_dates_fallback_to_natural_day_when_calendar_fails(monkeypatch):
    monkeypatch.setattr(
        tail_job,
        "_now",
        lambda: datetime(2026, 4, 27, 14, 10, tzinfo=tail_job.TZ),
    )

    def fake_window(*, end_calendar_day: date, trading_days: int) -> TradingWindow:
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(tail_job, "_resolve_trading_window", fake_window)
    prev_trade, today_trade = tail_job._resolve_trade_dates()
    assert prev_trade == "2026-04-26"
    assert today_trade == "2026-04-27"
