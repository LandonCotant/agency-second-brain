"""Unit tests for ``business_days_between``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agency_brain.agents.risk_watcher.business_days import business_days_between


def _dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def test_zero_when_endpoints_equal() -> None:
    """A zero-day span returns 0 even on a weekday."""
    assert business_days_between(_dt(2026, 5, 4), _dt(2026, 5, 4)) == 0


def test_monday_to_tuesday_returns_one() -> None:
    # 2026-05-04 is a Monday.
    assert business_days_between(_dt(2026, 5, 4), _dt(2026, 5, 5)) == 1


def test_monday_to_friday_returns_four() -> None:
    """Mon→Fri inclusive of the start, exclusive of the end = 4."""
    assert business_days_between(_dt(2026, 5, 4), _dt(2026, 5, 8)) == 4


def test_friday_to_monday_skips_weekend() -> None:
    """Fri→Mon = 1 business day (Friday counted, weekend skipped)."""
    assert business_days_between(_dt(2026, 5, 8), _dt(2026, 5, 11)) == 1


def test_saturday_to_monday_skips_weekend() -> None:
    """Sat→Mon = 0 (Saturday isn't counted; cursor advances over the
    weekend without incrementing)."""
    assert business_days_between(_dt(2026, 5, 9), _dt(2026, 5, 11)) == 0


def test_two_weeks_full_business_count() -> None:
    """Two full weeks Mon→Mon = 10 business days."""
    assert business_days_between(_dt(2026, 5, 4), _dt(2026, 5, 18)) == 10


def test_negative_when_start_after_end() -> None:
    assert business_days_between(_dt(2026, 5, 8), _dt(2026, 5, 4)) == -4


def test_sub_day_diff_does_not_advance_counter() -> None:
    """Within the same calendar day, the cursor never crosses to next."""
    start = _dt(2026, 5, 4, hour=9)
    end = start + timedelta(hours=4)
    assert business_days_between(start, end) == 0
