"""Unit tests for the Calendar client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agency_brain.agents.morning_brief.calendar_client import CalendarClient


class _FakeEvents:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict = {}

    def list(self, **kwargs):
        self.last_kwargs = kwargs
        return self

    def execute(self):
        return self._response


class _FakeService:
    def __init__(self, response):
        self._events = _FakeEvents(response)

    def events(self):
        return self._events


@dataclass
class _FakeFactory:
    response: dict = field(default_factory=lambda: {"items": []})
    last_subject: str = ""
    service: _FakeService | None = None

    def build(self, subject: str):
        self.last_subject = subject
        self.service = _FakeService(self.response)
        return self.service


def test_events_for_today_passes_subject_and_window():
    factory = _FakeFactory(response={"items": []})
    client = CalendarClient(service_factory=factory)
    events = client.events_for_today(
        "owner@example.com",
        date(2026, 5, 5),
        tz_name="America/Los_Angeles",
    )
    assert events == []
    assert factory.last_subject == "owner@example.com"
    kwargs = factory.service._events.last_kwargs  # type: ignore[union-attr]
    assert kwargs["calendarId"] == "primary"
    assert kwargs["singleEvents"] is True
    # Time window is in PT (UTC-7 during PDT in early May).
    assert kwargs["timeMin"].startswith("2026-05-05T00:00:00")
    assert kwargs["timeMax"].startswith("2026-05-05T23:59:59")


def test_events_for_today_parses_datetime_events():
    factory = _FakeFactory(
        response={
            "items": [
                {
                    "summary": "ClientC standup",
                    "start": {"dateTime": "2026-05-05T09:00:00-07:00"},
                    "end": {"dateTime": "2026-05-05T09:30:00-07:00"},
                    "attendees": [
                        {"email": "a@x.com"},
                        {"email": "b@x.com"},
                    ],
                }
            ]
        }
    )
    client = CalendarClient(service_factory=factory)
    events = client.events_for_today("owner@example.com", date(2026, 5, 5))
    assert len(events) == 1
    assert events[0].summary == "ClientC standup"
    assert events[0].attendees == ("a@x.com", "b@x.com")


def test_events_for_today_handles_all_day_events():
    factory = _FakeFactory(
        response={
            "items": [
                {
                    "summary": "Dentist",
                    "start": {"date": "2026-05-05"},
                    "end": {"date": "2026-05-06"},
                }
            ]
        }
    )
    client = CalendarClient(service_factory=factory)
    events = client.events_for_today(
        "owner@example.com",
        date(2026, 5, 5),
        tz_name="America/Los_Angeles",
    )
    assert len(events) == 1
    assert events[0].summary == "Dentist"


def test_events_for_today_returns_empty_on_api_error():
    class _Boom:
        def build(self, subject):
            class _S:
                def events(self_inner):
                    raise RuntimeError("api unavailable")

            return _S()

    client = CalendarClient(service_factory=_Boom())
    events = client.events_for_today("owner@example.com", date(2026, 5, 5))
    assert events == []


def test_unknown_tz_falls_back_to_utc():
    factory = _FakeFactory(response={"items": []})
    client = CalendarClient(service_factory=factory)
    events = client.events_for_today(
        "owner@example.com",
        date(2026, 5, 5),
        tz_name="Not/A_Real_TZ",
    )
    assert events == []
    # Window timeMin should still be a valid ISO string.
    kwargs = factory.service._events.last_kwargs  # type: ignore[union-attr]
    assert "2026-05-05" in kwargs["timeMin"]
