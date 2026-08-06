"""Unit tests for ``CalendarClient.most_recent_engagement_event`` (ADR 0035 §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.calendar_client import CalendarClient


@dataclass
class _StubRequest:
    items: list[dict[str, Any]]
    raise_exc: Exception | None

    def execute(self) -> Any:
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"items": self.items}


@dataclass
class _StubEvents:
    items: list[dict[str, Any]]
    raise_exc: Exception | None
    list_calls: list[dict[str, Any]]
    subject: str

    def list(self, **kwargs: Any) -> _StubRequest:
        self.list_calls.append({"subject": self.subject, **kwargs})
        return _StubRequest(items=self.items, raise_exc=self.raise_exc)


@dataclass
class _StubService:
    items: list[dict[str, Any]]
    raise_exc: Exception | None
    list_calls: list[dict[str, Any]]
    subject: str

    def events(self) -> _StubEvents:
        return _StubEvents(
            items=self.items,
            raise_exc=self.raise_exc,
            list_calls=self.list_calls,
            subject=self.subject,
        )


@dataclass
class _StubFactory:
    events: list[dict[str, Any]] = field(default_factory=list)
    raise_exc: Exception | None = None
    list_calls: list[dict[str, Any]] = field(default_factory=list)

    def build(self, subject: str) -> _StubService:
        return _StubService(
            items=self.events,
            raise_exc=self.raise_exc,
            list_calls=self.list_calls,
            subject=subject,
        )


def _event(
    *,
    start: str,
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "start": {"dateTime": start},
        "attendees": [{"email": e} for e in (attendees or [])],
    }


def test_returns_none_when_attendee_emails_empty() -> None:
    factory = _StubFactory()
    client = CalendarClient(service_factory=factory)
    assert (
        client.most_recent_engagement_event(
            owner_email="owner@example.com",
            attendee_emails=(),
            since=datetime(2026, 4, 1, tzinfo=UTC),
            until=datetime(2026, 5, 1, tzinfo=UTC),
        )
        is None
    )
    # Calendar service was never built — short-circuit before API call.
    assert factory.list_calls == []


def test_exact_email_match_only_no_domain_widening() -> None:
    """ADR 0035 §3: matching is exact-email, not domain-suffix.

    A different gmail.com user attending must NOT count as a match
    against a CRM contact at gmail.com — that was the v1 false-positive
    cause.
    """
    factory = _StubFactory(
        events=[
            _event(
                start="2026-04-25T15:00:00+00:00",
                attendees=["someone-else@gmail.com"],
            ),
        ],
    )
    client = CalendarClient(service_factory=factory)
    result = client.most_recent_engagement_event(
        owner_email="owner@example.com",
        attendee_emails=("client@gmail.com",),
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert result is None


def test_picks_most_recent_matching_event() -> None:
    factory = _StubFactory(
        events=[
            _event(
                start="2026-04-10T10:00:00+00:00",
                attendees=["client@clientapi.com"],
            ),
            _event(
                start="2026-04-20T10:00:00+00:00",
                attendees=["other@example.com"],  # no match
            ),
            _event(
                start="2026-04-25T10:00:00+00:00",
                attendees=["client@clientapi.com"],
            ),
        ],
    )
    client = CalendarClient(service_factory=factory)
    result = client.most_recent_engagement_event(
        owner_email="owner@example.com",
        attendee_emails=("client@clientapi.com",),
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert result == datetime(2026, 4, 25, 10, tzinfo=UTC)


def test_match_is_case_insensitive() -> None:
    factory = _StubFactory(
        events=[
            _event(
                start="2026-04-25T10:00:00+00:00",
                attendees=["Client@ClientAPI.com"],
            ),
        ],
    )
    client = CalendarClient(service_factory=factory)
    result = client.most_recent_engagement_event(
        owner_email="owner@example.com",
        attendee_emails=("client@clientapi.com",),
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert result == datetime(2026, 4, 25, 10, tzinfo=UTC)


def test_returns_none_on_api_failure() -> None:
    factory = _StubFactory(raise_exc=RuntimeError("calendar down"))
    client = CalendarClient(service_factory=factory)
    result = client.most_recent_engagement_event(
        owner_email="owner@example.com",
        attendee_emails=("client@clientapi.com",),
        since=datetime(2026, 4, 1, tzinfo=UTC),
        until=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert result is None
