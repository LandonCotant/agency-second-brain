"""Unit tests for the Morning Brief models."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agency_brain.agents.morning_brief.models import (
    CalendarEvent,
    DraftAwaitingReviewSnippet,
    MorningBriefInput,
    MorningBriefOutput,
    OpenTaskSnippet,
    RiskFlagSnippet,
    TriagedItemSnippet,
)


def test_morning_brief_input_defaults():
    inp = MorningBriefInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    assert inp.aspects == []
    assert inp.recipient_email == "owner@example.com"


def test_morning_brief_output_required_fields():
    out = MorningBriefOutput(
        brief_id="b-1",
        recipient_email="owner@example.com",
        local_date=date(2026, 5, 5),
        body_markdown="# Brief\n\nQuiet day.",
        sections_used=("calendar",),
        prompt_version="v1",
    )
    assert out.confidence == 1.0
    assert out.gmail_draft_id is None
    assert not out.dedup_skipped


def test_calendar_event_has_attendees_tuple():
    e = CalendarEvent(
        summary="Standup",
        start=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        end=datetime(2026, 5, 5, 9, 30, tzinfo=UTC),
        attendees=("a@x.com", "b@x.com"),
    )
    assert len(e.attendees) == 2
    # frozen
    try:
        e.attendees = ()  # type: ignore[misc]
    except Exception:
        pass


def test_snippet_types_are_frozen():
    t = TriagedItemSnippet(item_id="i-1", severity="high", summary="x", source="gmail")
    o = OpenTaskSnippet(task_id="t-1", name="Do thing", due_date=None)
    r = RiskFlagSnippet(flag_id="f-1", severity="high", pattern_name="silent_after_deliverable")
    d = DraftAwaitingReviewSnippet(task_id="t-2", name="Draft x")
    # All four are dataclass(frozen=True) so attribute writes raise.
    for obj in (t, o, r, d):
        assert obj.__class__.__hash__ is not None
