"""Tests for ``transform_event`` — markdown shape, embedding, metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.calendar_ingester.models import CalendarEvent
from agency_brain.agents.calendar_ingester.transformer import (
    CALENDAR_NOTE_KIND,
    EXTRACTION_METHOD,
    transform_event,
)


class _Embedder:
    def __init__(self, *, vector: list[float] | None) -> None:
        self.vector = vector
        self.calls: list[tuple[str, str]] = []

    def embed(self, *, text: str, model: str) -> list[float]:
        self.calls.append((text, model))
        if self.vector is None:
            raise RuntimeError("embedder boom")
        return list(self.vector)


def _ev(**kwargs) -> CalendarEvent:
    base = dict(
        event_id="ev1",
        calendar_id="primary",
        summary="Q2 review with Acme Corp",
        description="Discuss the proposal.",
        start=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 9, 11, 0, tzinfo=UTC),
        attendees=("sarah@acme.com",),
        organizer="owner@example.com",
        location="Zoom",
        status="confirmed",
        html_link="https://calendar.google.com/event?eid=foo",
    )
    base.update(kwargs)
    return CalendarEvent(**base)


def test_transform_renders_markdown_and_metadata() -> None:
    embedder = _Embedder(vector=[0.1] * 768)
    row = transform_event(event=_ev(), embedder=embedder)
    assert row is not None
    assert row.note_kind == CALENDAR_NOTE_KIND
    assert row.extraction_method == EXTRACTION_METHOD
    assert row.hipaa_isolated is False
    assert row.external_id == "ev1"
    assert "Q2 review" in row.markdown_content
    assert "sarah@acme.com" in row.markdown_content
    assert "Zoom" in row.markdown_content
    assert row.event_metadata.attendees == ("sarah@acme.com",)
    assert row.event_metadata.organizer == "owner@example.com"
    assert row.event_metadata.start_iso is not None
    assert row.event_metadata.status == "confirmed"
    assert len(row.embedding) == 768
    assert row.note_id.startswith("cal-")
    assert row.filename.startswith("[2026-05-09]")
    # revision_id falls back to event_id when updated_at is None
    assert row.revision_id == "ev1"


def test_transform_revision_id_uses_updated_at_when_present() -> None:
    """``revision_id`` is REQUIRED on agent_outputs.notes; it should
    track Calendar API's ``updated`` timestamp so re-running the
    ingester after an event edit produces a distinct revision."""
    embedder = _Embedder(vector=[0.1] * 768)
    updated = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)
    row = transform_event(event=_ev(updated_at=updated), embedder=embedder)
    assert row is not None
    assert row.revision_id == "2026-05-10T14:30:00+00:00"


def test_transform_returns_none_on_empty_event() -> None:
    """Truly content-less event: no summary, description, attendees,
    organizer, or location. Holding events with even a When+Location is
    still useful corpus content (you can ask "what did I have at Zoom
    last week?")."""
    embedder = _Embedder(vector=[0.1] * 768)
    ev = _ev(
        summary="",
        description="",
        attendees=(),
        organizer="",
        location="",
        start=None,
        end=None,
    )
    assert transform_event(event=ev, embedder=embedder) is None


def test_transform_tolerates_embed_failure() -> None:
    counter = [0]
    embedder = _Embedder(vector=None)  # raises
    row = transform_event(
        event=_ev(),
        embedder=embedder,
        embed_failures_counter=counter,
    )
    assert row is not None
    assert row.embedding == ()  # empty vector — VECTOR_SEARCH 768-pre-filter naturally skips
    assert counter[0] == 1


def test_transform_note_id_is_deterministic() -> None:
    """Same calendar_id + event_id MUST produce the same note_id —
    that's the MERGE-on-external_id stability guarantee."""
    embedder = _Embedder(vector=[0.1] * 768)
    a = transform_event(event=_ev(), embedder=embedder)
    b = transform_event(event=_ev(), embedder=embedder)
    assert a is not None and b is not None
    assert a.note_id == b.note_id
    assert a.external_id == b.external_id


def test_transform_includes_attendees_in_embed_text() -> None:
    """For VECTOR_SEARCH to answer "when did I last meet with X" queries
    (ADR 0046 verification 3a), the attendee email must be in the
    embedded text."""
    embedder = _Embedder(vector=[0.1] * 768)
    transform_event(event=_ev(), embedder=embedder)
    text, _ = embedder.calls[0]
    assert "sarah@acme.com" in text
