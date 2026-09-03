"""next_trading_day is the boundary the non-tradable list's expiry depends on
(backlog_2026_09_02 #1) -- getting the weekend skip wrong silently changes
"expires next trading day" back into "expires next calendar day"."""
from datetime import date

from momentum_scanner.session import next_trading_day


def test_weekday_rolls_to_next_weekday():
    assert next_trading_day(date(2026, 9, 7)) == date(2026, 9, 8)  # Mon -> Tue


def test_friday_skips_weekend_to_monday():
    assert next_trading_day(date(2026, 9, 4)) == date(2026, 9, 7)  # Fri -> Mon


def test_saturday_rolls_to_monday():
    assert next_trading_day(date(2026, 9, 5)) == date(2026, 9, 7)  # Sat -> Mon


def test_sunday_rolls_to_monday():
    assert next_trading_day(date(2026, 9, 6)) == date(2026, 9, 7)  # Sun -> Mon
