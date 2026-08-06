"""Tests for ``render_reflection_doc_body_html``."""

from __future__ import annotations

from datetime import date

from agency_brain.agents.evening_reflection.areas_context_reader import (
    AreaNoteSnippet,
)
from agency_brain.agents.evening_reflection.composer import (
    render_reflection_doc_body_html,
)
from agency_brain.agents.evening_reflection.models import (
    ExtractedDecision,
    ExtractedTodo,
    ExtractedWin,
    ReflectExtractionPayload,
)
from agency_brain.common.standard_questions import (
    Question,
    StandardQuestions,
)


def _payload(**overrides) -> ReflectExtractionPayload:
    base = {
        "commentary": "#### What happened today\nA quiet day.\n\n#### What it might mean\nNot much.\n\n#### Worth carrying into tomorrow\nWhat's the next move?",
        "decisions": (),
        "wins": (),
        "todos": (),
        "custom_questions": (),
    }
    base.update(overrides)
    return ReflectExtractionPayload(**base)


def test_renders_signals_questions_and_commentary() -> None:
    qs = StandardQuestions(
        questions=(
            Question(id="a", prompt="What worked?"),
            Question(id="b", prompt="What didn't?"),
        ),
        mode="all",
    )
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(custom_questions=("What's the lever for the ClientC brief?",)),
        standard_questions=qs,
        completed_tasks_block="- T1\n- T2",
        triaged_today_block="(none)",
        calendar_block="- 09:00-10:00: standup",
        morning_brief_block="(none)",
        active_risk_flags_block="(none)",
        voice_memos_block="(none)",
    )
    assert "<h1>Evening Reflection — Thursday, May 07, 2026</h1>" in html
    assert "<i>For the operator.</i>" in html
    assert "<h3>Tasks completed today</h3>" in html
    assert "<li>T1</li>" in html and "<li>T2</li>" in html
    assert "<h2>Reflection commentary</h2>" in html
    assert "<h4>What happened today</h4>" in html
    assert "What worked?" in html and "What didn&#x27;t?" in html
    assert "<h2>Custom reflection questions</h2>" in html
    assert "ClientC brief" in html
    assert "<h2>Your reflection</h2>" in html


def test_extraction_summary_renders_decisions_wins_todos() -> None:
    payload = _payload(
        decisions=(ExtractedDecision(title="Stop chasing X", context="see memo"),),
        wins=(ExtractedWin(title="Closed Y", summary="quietly"),),
        todos=(ExtractedTodo(body="Email Z"),),
    )
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=payload,
        standard_questions=None,
    )
    assert "<h4>Decisions</h4>" in html
    assert "Stop chasing X" in html
    assert "<h4>Wins</h4>" in html
    assert "<h4>Todos</h4>" in html
    assert "Email Z" in html


def test_empty_extraction_renders_none_message() -> None:
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(),
        standard_questions=None,
    )
    assert "no decisions, wins, or todos extracted today" in html
    assert "(none surfaced today)" in html  # custom questions empty


def test_areas_context_renders_titles_and_snippets_with_links() -> None:
    areas = (
        AreaNoteSnippet(
            note_id="n1",
            filename="clienta-pi-dossier.gdoc",
            snippet="Client A is a private investigation firm in Vegas.",
            distance=0.21,
            source_drive_url="https://drive.google.com/n1",
        ),
        AreaNoteSnippet(
            note_id="n2",
            filename="ls-onboarding.gdoc",
            snippet="Local service onboarding playbook v1.",
            distance=0.27,
        ),
    )
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(),
        standard_questions=None,
        areas_context=areas,
    )
    assert "<h2>Areas context</h2>" in html
    assert "clienta-pi-dossier.gdoc" in html
    assert "https://drive.google.com/n1" in html
    assert "Local service onboarding playbook" in html
    assert "distance 0.210" in html
    assert "distance 0.270" in html


def test_areas_context_empty_renders_none_message() -> None:
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(),
        standard_questions=None,
        areas_context=(),
    )
    assert "<h2>Areas context</h2>" in html
    assert "(none surfaced)" in html


def test_areas_context_none_param_renders_none_message() -> None:
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(),
        standard_questions=None,
    )
    # Default areas_context=None should still render the section as none.
    assert "<h2>Areas context</h2>" in html


def test_signals_block_with_html_unsafe_chars_is_escaped() -> None:
    html = render_reflection_doc_body_html(
        recipient_name="the operator",
        run_date=date(2026, 5, 7),
        payload=_payload(),
        standard_questions=None,
        triaged_today_block="<script>alert(1)</script>",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
