"""Business-day arithmetic for Risk Watcher signals.

Mon-Fri only — no holiday calendar (a 2-person agency in the US doesn't
have an enforced corporate holiday list, and a holiday-aware version
would either depend on an external service or hardcode US federal
holidays without per-client tuning, neither of which earns the
ceremony for a high-recall signal).

Both functions are time-zone-agnostic — callers pass tz-aware
datetimes. The weekday check is on `.weekday()`, which is timezone-
neutral once the datetime is anchored.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def business_days_between(start: datetime, end: datetime) -> int:
    """Return the number of business days from ``start`` to ``end``.

    Both endpoints are timezone-aware. The result counts the START day
    if it's a business day; the END day is excluded — i.e. a Monday
    start and a Tuesday end of the same week returns 1.

    Negative if ``start > end``.
    """
    if start > end:
        return -business_days_between(end, start)

    days = 0
    cursor = start
    while cursor.date() < end.date():
        if cursor.weekday() < 5:  # Mon=0..Fri=4
            days += 1
        cursor = cursor + timedelta(days=1)
    return days
