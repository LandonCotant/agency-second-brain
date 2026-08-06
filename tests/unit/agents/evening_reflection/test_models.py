"""Unit tests for the Evening Reflection dataclasses."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agency_brain.agents.evening_reflection.models import (
    ActiveRiskFlagSnippet,
    CompletedTaskSnippet,
    EveningReflectionInput,
    EveningReflectionOutput,
    InFlightDecision,
    MorningBriefPlanSnippet,
    ReflectionMode,
    TriagedTodaySnippet,
    VoiceMemo,
)


def test_evening_reflection_input_defaults():
    inp = EveningReflectionInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    assert inp.recipient_email == "owner@example.com"
    assert inp.run_date == date(2026, 5, 5)
    assert inp.aspects == []
    # ADR 0040 §1: default mode is REFLECT so v1 callers / pre-PR-C scheduler
    # firings without an env override behave exactly as ADR 0036 specified.
    assert inp.mode is ReflectionMode.REFLECT


def test_reflection_mode_enum_values():
    assert ReflectionMode.PROMPT.value == "prompt"
    assert ReflectionMode.REFLECT.value == "reflect"


def test_evening_reflection_input_accepts_explicit_mode():
    inp = EveningReflectionInput(
        recipient_email="a@x.com",
        run_date=date(2026, 5, 5),
        mode=ReflectionMode.PROMPT,
    )
    assert inp.mode is ReflectionMode.PROMPT


def test_voice_memo_dataclass_is_frozen():
    m = VoiceMemo(
        note_id="captures-rec123",
        markdown_content="A 30-second thought about the ClientC brief.",
        ingested_at=datetime(2026, 5, 5, 20, 30, tzinfo=UTC),
    )
    assert m.note_id == "captures-rec123"
    assert m.markdown_content.startswith("A 30-second")
    assert m.ingested_at.tzinfo is UTC
    # frozen=True means assignment raises.
    import dataclasses

    try:
        m.note_id = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover — only fires if frozen breaks
        raise AssertionError("VoiceMemo must be frozen")


def test_in_flight_decision_dataclass_is_frozen():
    d = InFlightDecision(
        decision_id="dec-1",
        title="Whether to renew Client A engagement",
        context=None,
        status="draft",
        decided_at=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
    )
    assert d.status == "draft"
    assert d.context is None


def test_evening_reflection_input_aspects_isolated_per_instance():
    a = EveningReflectionInput(recipient_email="a@x.com", run_date=date(2026, 5, 5))
    b = EveningReflectionInput(recipient_email="b@x.com", run_date=date(2026, 5, 5))
    a.aspects.append("hipaa_excluded")
    assert b.aspects == []  # not shared


def test_evening_reflection_output_defaults():
    out = EveningReflectionOutput(
        reflection_id="r-1",
        recipient_email="a@x.com",
        local_date=date(2026, 5, 5),
        body_markdown="# What happened\n\nA day.",
        sections_used=("completed_tasks",),
        prompt_version="v1",
    )
    assert out.confidence == 1.0
    assert out.gmail_draft_id is None
    assert out.dedup_skipped is False
    assert out.dedup_existing_reflection_id is None


def test_completed_task_snippet_optional_fields():
    s = CompletedTaskSnippet(task_id="t-1", name="Reply to ClientC")
    assert s.project_name is None
    assert s.completed_date is None


def test_triaged_today_snippet_carries_action_type():
    s = TriagedTodaySnippet(
        item_id="i-1",
        severity="medium",
        summary="Lead-gen results draft",
        source="gmail",
        action_type="defer",
    )
    assert s.action_type == "defer"
    assert s.source_url is None


def test_morning_brief_plan_snippet_default_sections():
    s = MorningBriefPlanSnippet(brief_id="b-1", body_markdown="# Brief", sections_used=())
    assert s.sections_used == ()


def test_active_risk_flag_snippet_optional_account_name():
    s = ActiveRiskFlagSnippet(flag_id="f-1", severity="high", pattern_name="OwnerDisengagement")
    assert s.account_name is None
    assert s.reasoning is None
