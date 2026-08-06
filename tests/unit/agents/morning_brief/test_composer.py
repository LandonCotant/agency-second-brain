"""Unit tests for the Morning Brief composer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from agency_brain.agents.morning_brief.composer import (
    MorningBriefComposer,
    render_section_blocks,
)
from agency_brain.agents.morning_brief.models import (
    CalendarEvent,
    DraftAwaitingReviewSnippet,
    OpenTaskSnippet,
    RiskFlagSnippet,
    TriagedItemSnippet,
)


@dataclass
class _FakeLLM:
    """Captures the prompt and returns a canned response."""

    response: str = "# Morning Brief\n\nQuiet day — calendar clear, no new triage."
    last_prompt: str = ""

    def compose(self, *, prompt: str, model: str) -> str:
        self.last_prompt = prompt
        return self.response


_TEMPLATE = (
    "Recipient: {{recipient_email}}, Name: {{recipient_name}}, "
    "Date: {{run_date_human}}\n"
    "Triaged:\n{{triaged_items_block}}\n"
    "Tasks:\n{{open_tasks_block}}\n"
    "Risk:\n{{risk_flags_block}}\n"
    "Drafts:\n{{drafts_awaiting_block}}\n"
    "Calendar:\n{{calendar_block}}\n"
)


def test_compose_substitutes_all_placeholders():
    llm = _FakeLLM()
    composer = MorningBriefComposer(prompt_template=_TEMPLATE, llm=llm)
    body = composer.compose(
        recipient_email="owner@example.com",
        recipient_name="the operator",
        run_date=date(2026, 5, 5),
        triaged_items_block="(none)",
        open_tasks_block="(none)",
        risk_flags_block="(none)",
        drafts_awaiting_block="(none)",
        calendar_block="(none)",
    )
    assert "Quiet day" in body
    assert "owner@example.com" in llm.last_prompt
    assert "the operator" in llm.last_prompt
    assert "Tuesday, May 05, 2026" in llm.last_prompt


def test_compose_falls_back_when_llm_returns_empty():
    llm = _FakeLLM(response="")
    composer = MorningBriefComposer(prompt_template=_TEMPLATE, llm=llm)
    body = composer.compose(
        recipient_email="x@y.com",
        recipient_name="X",
        run_date=date(2026, 5, 5),
        triaged_items_block="(none)",
        open_tasks_block="(none)",
        risk_flags_block="(none)",
        drafts_awaiting_block="(none)",
        calendar_block="(none)",
    )
    assert "Quiet day" in body
    assert "Tuesday, May 05" in body


def test_render_section_blocks_empty_inputs():
    blocks = render_section_blocks(
        triaged_items=[],
        open_tasks=[],
        risk_flags=[],
        drafts_awaiting=[],
        calendar_events=[],
    )
    assert blocks == {
        "triaged_items_block": "(none)",
        "open_tasks_block": "(none)",
        "risk_flags_block": "(none)",
        "drafts_awaiting_block": "(none)",
        "calendar_block": "(none)",
    }


def test_render_section_blocks_full_inputs():
    blocks = render_section_blocks(
        triaged_items=[
            TriagedItemSnippet(
                item_id="i1",
                severity="critical",
                summary="Client A escalation",
                source="gmail",
                source_url="https://mail.google.com/x",
            ),
        ],
        open_tasks=[
            OpenTaskSnippet(
                task_id="t1",
                name="Reply to ClientC",
                due_date=date(2026, 5, 6),
                project_name="Client C Studio",
            )
        ],
        risk_flags=[
            RiskFlagSnippet(
                flag_id="f1",
                severity="high",
                pattern_name="silent_after_deliverable",
                account_name="Client A",
                reasoning="No response in 14 days.",
            )
        ],
        drafts_awaiting=[
            DraftAwaitingReviewSnippet(
                task_id="t2",
                name="Q3 lead-gen results",
                project_name="Triage Inbox",
            )
        ],
        calendar_events=[
            CalendarEvent(
                summary="ClientC standup",
                start=datetime(2026, 5, 5, 16, 0, tzinfo=UTC),
                end=datetime(2026, 5, 5, 16, 30, tzinfo=UTC),
                attendees=("a@x.com",),
            )
        ],
        timezone="America/Los_Angeles",
    )
    assert "[critical] Client A escalation" in blocks["triaged_items_block"]
    assert "https://mail.google.com/x" in blocks["triaged_items_block"]
    assert "Reply to ClientC (due 2026-05-06)" in blocks["open_tasks_block"]
    assert "Client C Studio" in blocks["open_tasks_block"]
    assert "[high] silent_after_deliverable on Client A" in blocks["risk_flags_block"]
    assert "Q3 lead-gen results" in blocks["drafts_awaiting_block"]
    # 16:00 UTC = 09:00 PT
    assert "09:00–09:30: ClientC standup" in blocks["calendar_block"]


def test_render_calendar_event_with_many_attendees_shows_count():
    blocks = render_section_blocks(
        triaged_items=[],
        open_tasks=[],
        risk_flags=[],
        drafts_awaiting=[],
        calendar_events=[
            CalendarEvent(
                summary="All-hands",
                start=datetime(2026, 5, 5, 17, 0, tzinfo=UTC),
                end=datetime(2026, 5, 5, 18, 0, tzinfo=UTC),
                attendees=tuple(f"u{i}@x.com" for i in range(7)),
            )
        ],
    )
    assert "(7 attendees)" in blocks["calendar_block"]
