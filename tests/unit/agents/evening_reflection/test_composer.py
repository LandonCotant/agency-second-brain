"""Unit tests for the Evening Reflection composer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from agency_brain.agents.evening_reflection.composer import (
    EveningReflectionComposer,
    render_section_blocks,
)
from agency_brain.agents.evening_reflection.models import (
    ActiveRiskFlagSnippet,
    CompletedTaskSnippet,
    MorningBriefPlanSnippet,
    TriagedTodaySnippet,
)
from agency_brain.agents.morning_brief.models import CalendarEvent


@dataclass
class _FakeLLM:
    """Captures the prompt and returns a canned response."""

    response: str = (
        "### What happened today\n\n"
        "Three tasks closed; calendar was light.\n\n"
        "### What it might mean\n\n"
        "Quiet day, real progress.\n\n"
        "### Worth carrying into tomorrow\n\n"
        "What's the one thing worth pulling forward?"
    )
    last_prompt: str = ""

    def compose(self, *, prompt: str, model: str) -> str:
        self.last_prompt = prompt
        return self.response


_TEMPLATE = (
    "Recipient: {{recipient_email}}, Name: {{recipient_name}}, "
    "Date: {{run_date_human}}\n"
    "Completed:\n{{completed_tasks_block}}\n"
    "Triaged:\n{{triaged_today_block}}\n"
    "Calendar:\n{{calendar_block}}\n"
    "Brief:\n{{morning_brief_block}}\n"
    "Risk:\n{{active_risk_flags_block}}\n"
)


def test_compose_substitutes_all_placeholders():
    llm = _FakeLLM()
    composer = EveningReflectionComposer(prompt_template=_TEMPLATE, llm=llm)
    body = composer.compose(
        recipient_email="owner@example.com",
        recipient_name="the operator",
        run_date=date(2026, 5, 5),
        completed_tasks_block="(none)",
        triaged_today_block="(none)",
        calendar_block="(none)",
        morning_brief_block="(none)",
        active_risk_flags_block="(none)",
    )
    assert "What happened today" in body
    assert "What it might mean" in body
    assert "Worth carrying into tomorrow" in body
    assert "owner@example.com" in llm.last_prompt
    assert "the operator" in llm.last_prompt
    assert "Tuesday, May 05, 2026" in llm.last_prompt


def test_compose_falls_back_to_unremarkable_paragraph_when_llm_returns_empty():
    llm = _FakeLLM(response="")
    composer = EveningReflectionComposer(prompt_template=_TEMPLATE, llm=llm)
    body = composer.compose(
        recipient_email="x@y.com",
        recipient_name="X",
        run_date=date(2026, 5, 5),
        completed_tasks_block="(none)",
        triaged_today_block="(none)",
        calendar_block="(none)",
        morning_brief_block="(none)",
        active_risk_flags_block="(none)",
    )
    # Tone-aligned with spec: paragraph, not a "Quiet day" one-liner.
    assert "unremarkable day" in body
    assert "Tuesday, May 05" in body
    assert "\n\n" not in body  # single paragraph


def test_compose_falls_back_when_llm_returns_whitespace_only():
    llm = _FakeLLM(response="   \n\n   ")
    composer = EveningReflectionComposer(prompt_template=_TEMPLATE, llm=llm)
    body = composer.compose(
        recipient_email="x@y.com",
        recipient_name="X",
        run_date=date(2026, 5, 5),
        completed_tasks_block="(none)",
        triaged_today_block="(none)",
        calendar_block="(none)",
        morning_brief_block="(none)",
        active_risk_flags_block="(none)",
    )
    assert "unremarkable day" in body


def test_render_section_blocks_empty_inputs():
    blocks = render_section_blocks(
        completed_tasks=[],
        triaged_today=[],
        calendar_events=[],
        morning_brief=None,
        active_risk_flags=[],
    )
    # Every v1 + ADR-0040 block renders "(none)" for empty inputs.
    expected_keys = {
        "completed_tasks_block",
        "triaged_today_block",
        "calendar_block",
        "morning_brief_block",
        "active_risk_flags_block",
        "voice_memos_block",
        "in_flight_decisions_block",
        "open_followups_block",
    }
    assert set(blocks.keys()) == expected_keys
    for value in blocks.values():
        assert value == "(none)"


def test_render_section_blocks_full_inputs():
    blocks = render_section_blocks(
        completed_tasks=[
            CompletedTaskSnippet(
                task_id="t1", name="Reply to ClientC", project_name="Client C Studio"
            ),
            CompletedTaskSnippet(task_id="t2", name="Quick errand"),
        ],
        triaged_today=[
            TriagedTodaySnippet(
                item_id="i1",
                severity="critical",
                summary="Client A escalation",
                source="gmail",
                action_type="do_now",
                source_url="https://mail.google.com/x",
            ),
            TriagedTodaySnippet(
                item_id="i2",
                severity="medium",
                summary="Q3 lead-gen results draft",
                source="gmail",
                action_type="defer",
            ),
        ],
        calendar_events=[
            CalendarEvent(
                summary="ClientC standup",
                start=datetime(2026, 5, 5, 16, 0, tzinfo=UTC),
                end=datetime(2026, 5, 5, 16, 30, tzinfo=UTC),
                attendees=("a@x.com",),
            )
        ],
        morning_brief=MorningBriefPlanSnippet(
            brief_id="b1",
            body_markdown="# Top 3\n- Reply to ClientC\n- Plan Q3\n",
            sections_used=("top_three",),
        ),
        active_risk_flags=[
            ActiveRiskFlagSnippet(
                flag_id="f1",
                severity="high",
                pattern_name="silent_after_deliverable",
                account_name="Client A",
                reasoning="No response in 14 days.",
            )
        ],
        timezone="America/Los_Angeles",
    )
    assert "Reply to ClientC / Client C Studio" in blocks["completed_tasks_block"]
    assert "Quick errand" in blocks["completed_tasks_block"]
    # Critical first, action_type rendered.
    assert "[critical] Client A escalation" in blocks["triaged_today_block"]
    assert "action=do_now" in blocks["triaged_today_block"]
    assert "https://mail.google.com/x" in blocks["triaged_today_block"]
    # Medium-severity carried (unlike morning brief which filtered).
    assert "[medium] Q3 lead-gen results draft" in blocks["triaged_today_block"]
    # 16:00 UTC = 09:00 PT.
    assert "09:00-09:30: ClientC standup" in blocks["calendar_block"]
    # Morning brief surfaced verbatim.
    assert "Top 3" in blocks["morning_brief_block"]
    assert "Reply to ClientC" in blocks["morning_brief_block"]
    # Risk flag has account + reasoning.
    assert "[high] silent_after_deliverable on Client A" in blocks["active_risk_flags_block"]
    assert "No response in 14 days" in blocks["active_risk_flags_block"]


def test_render_morning_brief_none_renders_none_marker():
    blocks = render_section_blocks(
        completed_tasks=[],
        triaged_today=[],
        calendar_events=[],
        morning_brief=None,
        active_risk_flags=[],
    )
    assert blocks["morning_brief_block"] == "(none)"


def test_render_morning_brief_with_empty_body_renders_none_marker():
    blocks = render_section_blocks(
        completed_tasks=[],
        triaged_today=[],
        calendar_events=[],
        morning_brief=MorningBriefPlanSnippet(brief_id="b1", body_markdown="", sections_used=()),
        active_risk_flags=[],
    )
    assert blocks["morning_brief_block"] == "(none)"


def test_render_calendar_event_with_many_attendees_shows_count():
    blocks = render_section_blocks(
        completed_tasks=[],
        triaged_today=[],
        calendar_events=[
            CalendarEvent(
                summary="All-hands",
                start=datetime(2026, 5, 5, 17, 0, tzinfo=UTC),
                end=datetime(2026, 5, 5, 18, 0, tzinfo=UTC),
                attendees=tuple(f"u{i}@x.com" for i in range(7)),
            )
        ],
        morning_brief=None,
        active_risk_flags=[],
    )
    assert "(7 attendees)" in blocks["calendar_block"]


def test_render_triaged_today_no_action_type():
    blocks = render_section_blocks(
        completed_tasks=[],
        triaged_today=[
            TriagedTodaySnippet(
                item_id="i1",
                severity="info",
                summary="FYI item",
                source="airtable",
                action_type="",
            )
        ],
        calendar_events=[],
        morning_brief=None,
        active_risk_flags=[],
    )
    # Empty action_type means no ", action=..." suffix.
    assert "action=" not in blocks["triaged_today_block"]
    assert "[info] FYI item (source=airtable)" in blocks["triaged_today_block"]
