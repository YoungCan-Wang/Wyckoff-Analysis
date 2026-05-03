"""utils/trading_clock.py 冒烟测试。"""

from __future__ import annotations

from datetime import date, datetime

from utils.trading_clock import CN_TZ, resolve_end_calendar_day


class TestResolveEndCalendarDay:
    def test_before_switch_hour_returns_previous_day(self):
        """16 点前应返回前一天。"""
        morning = datetime(2024, 3, 15, 9, 0, tzinfo=CN_TZ)
        result = resolve_end_calendar_day(morning, switch_hour=16)
        assert result == date(2024, 3, 14)

    def test_after_switch_hour_returns_today(self):
        """16 点后应返回当天。"""
        evening = datetime(2024, 3, 15, 17, 0, tzinfo=CN_TZ)
        result = resolve_end_calendar_day(evening, switch_hour=16)
        assert result == date(2024, 3, 15)

    def test_returns_date_type(self):
        result = resolve_end_calendar_day(datetime(2024, 6, 1, 20, 0, tzinfo=CN_TZ))
        assert isinstance(result, date)
