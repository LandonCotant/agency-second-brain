"""Tests for ``CalendarClient`` — pagination + event normalization."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.calendar_ingester.calendar_client import (
    CalendarClient,
    _normalize_event,
)


class _StubFactory:
    def __init__(self, *, service) -> None:
        self.service = service
        self.last_subject: str | None = None

    def build(self, subject: str):
        self.last_subject = subject
        return self.service


class _StubExecuteable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _StubService:
    def __init__(self, *, pages: list[dict]) -> None:
        self._pages = list(pages)
        self.list_calls: list[dict] = []

    def events(self):
        return self

    def list(self, **kwargs) -> _StubExecuteable:
        self.list_calls.append(kwargs)
        page = self._pages.pop(0) if self._pages else {}
        return _StubExecuteable(page)


def test_list_events_paginates() -> None:
    pages = [
        {
            "items": [
                {
                    "id": "ev1",
                    "summary": "Sync with Sarah",
                    "start": {"dateTime": "2026-05-09T10:00:00Z"},
                    "end": {"dateTime": "2026-05-09T10:30:00Z"},
                    "attendees": [{"email": "sarah@example.com"}],
                    "organizer": {"email": "owner@example.com"},
                    "status": "confirmed",
                }
            ],
            "nextPageToken": "p2",
        },
        {
            "items": [
                {
                    "id": "ev2",
                    "summary": "Q2 review",
                    "start": {"dateTime": "2026-05-10T14:00:00Z"},
                    "end": {"dateTime": "2026-05-10T15:00:00Z"},
                    "attendees": [],
                    "status": "confirmed",
                }
            ],
        },
    ]
    factory = _StubFactory(service=_StubService(pages=pages))
    client = CalendarClient(factory=factory, subject="owner@example.com")
    events = client.list_events(
        calendar_id="primary",
        time_min=datetime(2026, 5, 1, tzinfo=UTC),
        time_max=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert [e.event_id for e in events] == ["ev1", "ev2"]
    assert events[0].summary == "Sync with Sarah"
    assert events[0].attendees == ("sarah@example.com",)
    assert events[0].organizer == "owner@example.com"
    assert factory.last_subject == "owner@example.com"
    # Both pages requested.
    list_calls = factory.service.list_calls
    assert len(list_calls) == 2
    assert list_calls[0]["singleEvents"] is True
    assert list_calls[0]["showDeleted"] is False


def test_list_events_skips_cancelled() -> None:
    pages = [
        {
            "items": [
                {"id": "ev1", "summary": "(cancelled)", "status": "cancelled"},
                {
                    "id": "ev2",
                    "summary": "Live event",
                    "status": "confirmed",
                    "start": {"dateTime": "2026-05-10T14:00:00Z"},
                    "end": {"dateTime": "2026-05-10T15:00:00Z"},
                },
            ]
        }
    ]
    factory = _StubFactory(service=_StubService(pages=pages))
    client = CalendarClient(factory=factory, subject="owner@example.com")
    events = client.list_events(
        calendar_id="primary",
        time_min=datetime(2026, 5, 1, tzinfo=UTC),
        time_max=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert [e.event_id for e in events] == ["ev2"]


def test_normalize_event_decodes_all_day() -> None:
    raw = {
        "id": "ev1",
        "summary": "Holiday",
        "status": "confirmed",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-05"},
    }
    ev = _normalize_event(raw, calendar_id="primary")
    assert ev is not None
    assert ev.start is not None
    assert ev.start.year == 2026 and ev.start.month == 7 and ev.start.day == 4


def test_normalize_event_lowercases_emails() -> None:
    raw = {
        "id": "ev1",
        "summary": "Sync",
        "status": "confirmed",
        "start": {"dateTime": "2026-05-09T10:00:00Z"},
        "end": {"dateTime": "2026-05-09T10:30:00Z"},
        "attendees": [
            {"email": "Sarah.Chen@Example.com"},
            {"email": "BOB@OTHER.COM"},
        ],
        "organizer": {"email": "Owner@Example.com"},
    }
    ev = _normalize_event(raw, calendar_id="primary")
    assert ev.attendees == ("sarah.chen@example.com", "bob@other.com")
    assert ev.organizer == "owner@example.com"


def test_normalize_event_returns_none_on_no_id() -> None:
    raw = {"summary": "Bare event"}
    assert _normalize_event(raw, calendar_id="primary") is None
