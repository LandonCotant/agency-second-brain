"""Tests for the Calendar HIPAA filter."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.calendar_ingester.hipaa_filter import (
    CalendarHipaaFilter,
    load_hipaa_domains_from_bq,
)
from agency_brain.agents.calendar_ingester.models import CalendarEvent


def _ev(*, attendees: tuple[str, ...] = (), organizer: str = "ok@example.com") -> CalendarEvent:
    return CalendarEvent(
        event_id="ev1",
        calendar_id="primary",
        summary="Sync",
        description="",
        start=datetime(2026, 5, 9, tzinfo=UTC),
        end=datetime(2026, 5, 9, tzinfo=UTC),
        attendees=attendees,
        organizer=organizer,
        location="",
        status="confirmed",
        html_link="",
    )


def test_check_allows_no_hipaa_attendees() -> None:
    f = CalendarHipaaFilter(hipaa_domains=("hospitalcorp.com",))
    result = f.check(_ev(attendees=("ok@example.com",)))
    assert result.allowed is True
    assert result.blocking_attendees == ()


def test_check_blocks_hipaa_attendee() -> None:
    f = CalendarHipaaFilter(hipaa_domains=("hospitalcorp.com",))
    result = f.check(_ev(attendees=("doc@hospitalcorp.com",)))
    assert result.allowed is False
    assert "doc@hospitalcorp.com" in result.blocking_attendees


def test_check_blocks_hipaa_organizer() -> None:
    f = CalendarHipaaFilter(hipaa_domains=("hospitalcorp.com",))
    result = f.check(_ev(organizer="admin@hospitalcorp.com"))
    assert result.allowed is False


def test_check_empty_domains_passes_everything() -> None:
    f = CalendarHipaaFilter(hipaa_domains=())
    result = f.check(_ev(attendees=("anyone@example.com", "two@x.com")))
    assert result.allowed is True


# ----------------------------------------------------------- BQ loader


class _FakeBQ:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def query_rows(self, sql: str, parameters=None):
        return list(self.rows)


def test_load_hipaa_domains_extracts_unique() -> None:
    bq = _FakeBQ(
        rows=[
            {"google_group_email": "x@hospitalcorp.com", "website": "https://hospitalcorp.com"},
            {"google_group_email": None, "website": "https://www.healthplus.org"},
        ]
    )
    domains = load_hipaa_domains_from_bq(bq_query=bq, project_id="p")
    assert "hospitalcorp.com" in domains
    assert "healthplus.org" in domains
    assert len(domains) == 2
