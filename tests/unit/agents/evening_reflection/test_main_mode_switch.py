"""Unit tests for the ADR 0040 ``REFLECTION_MODE`` dispatch.

Covers two layers:

1. ``resolve_mode`` + ``resolve_prompt_version`` — the small pure
   helpers in ``main.py`` that translate the env var into a
   ``ReflectionMode`` + the matching prompt-template version. These
   stay pure so they're testable without booting the lazy-imported BQ
   / Vertex / Gmail clients.

2. ``EveningReflectionAgent._run`` mode dispatch — when the input is
   ``ReflectionMode.PROMPT``, the agent must call the
   in-flight-decisions reader and skip the voice-memos reader (and
   vice versa). Subject prefix differs between modes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from agency_brain.agents.evening_reflection.agent import EveningReflectionAgent
from agency_brain.agents.evening_reflection.composer import (
    EveningReflectionComposer,
)
from agency_brain.agents.evening_reflection.main import (
    resolve_mode,
    resolve_prompt_version,
)
from agency_brain.agents.evening_reflection.models import (
    EveningReflectionInput,
    InFlightDecision,
    ReflectionMode,
    VoiceMemo,
)
from agency_brain.agents.evening_reflection.readers import (
    ActiveRiskFlagsTodayReader,
    CompletedTasksTodayReader,
    MorningBriefForTodayReader,
    TriagedItemsTodayReader,
)
from agency_brain.agents.evening_reflection.writer import EveningReflectionWriter
from agency_brain.agents.morning_brief.calendar_client import CalendarClient
from agency_brain.agents.morning_brief.gmail_drafts_client import GmailDraftsClient
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank

# --------------------------------------------------- helper resolution


def test_resolve_mode_default_is_reflect_when_unset():
    assert resolve_mode(None) is ReflectionMode.REFLECT


def test_resolve_mode_default_is_reflect_when_empty():
    assert resolve_mode("") is ReflectionMode.REFLECT
    assert resolve_mode("   ") is ReflectionMode.REFLECT


def test_resolve_mode_recognizes_prompt_case_insensitively():
    assert resolve_mode("prompt") is ReflectionMode.PROMPT
    assert resolve_mode("PROMPT") is ReflectionMode.PROMPT
    assert resolve_mode(" Prompt ") is ReflectionMode.PROMPT


def test_resolve_mode_explicit_reflect_returns_reflect():
    assert resolve_mode("reflect") is ReflectionMode.REFLECT


def test_resolve_mode_unknown_value_falls_back_to_reflect():
    """Fail-closed: a typo in the scheduler containerOverrides body
    does NOT silently swap to PROMPT; it stays on REFLECT (the
    pre-PR-C default)."""
    assert resolve_mode("anchor") is ReflectionMode.REFLECT
    assert resolve_mode("p") is ReflectionMode.REFLECT
    assert resolve_mode("promptmode") is ReflectionMode.REFLECT


def test_resolve_prompt_version_maps_per_mode():
    assert resolve_prompt_version(ReflectionMode.PROMPT) == "prompt_v1"
    # ADR 0044 — REFLECT mode loads the doc-flavored prompt template that
    # also instructs the LLM to emit `custom_questions` for the Doc body.
    assert resolve_prompt_version(ReflectionMode.REFLECT) == "reflect_doc_v1"


# --------------------------------------------------- agent-level mode dispatch


@dataclass
class _FakeBQRows:
    captured: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.captured.append((table_ref, rows))
        return []


@dataclass
class _FakeDedup:
    existing: dict = field(default_factory=dict)

    def find_existing_reflection_id(self, table_ref, recipient_email, local_date, mode="reflect"):
        return self.existing.get((recipient_email, local_date.isoformat()))


@dataclass
class _FakeAuditClient:
    emitted: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.emitted.append((table_ref, rows))
        return []


class _FakeLLM:
    def compose(self, *, prompt: str, model: str) -> str:
        return "(rendered body)"


class _StubCalendarFactory:
    def build(self, subject: str):
        class _S:
            def events(self_inner):
                class _E:
                    def list(self_inner2, **kwargs):
                        return self_inner2

                    def execute(self_inner2):
                        return {"items": []}

                return _E()

        return _S()


class _StubGmailFactory:
    def __init__(self) -> None:
        self.last_subject = ""
        self.last_body: dict = {}

    def build(self, subject: str):
        outer = self

        class _S:
            def users(self_inner):
                class _U:
                    def drafts(self_inner2):
                        class _D:
                            def create(self_inner3, *, userId, body):
                                outer.last_body = body
                                return self_inner3

                            def execute(self_inner3):
                                return {"id": "r-draft-id"}

                        return _D()

                return _U()

        return _S()


class _CountingReader:
    """Records load() calls + lets tests assert call count."""

    def __init__(self, return_value):
        self._value = return_value
        self.call_count = 0

    def load(self, *args, **kwargs):
        self.call_count += 1
        return list(self._value) if isinstance(self._value, list) else self._value


@dataclass
class _NoopBQ:
    """A BQ stub that swallows queries and always returns []."""

    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return []


def _build_agent(
    *,
    voice_memos_reader=None,
    in_flight_decisions_reader=None,
    triaged_reader=None,
):
    bq_rows = _NoopBQ()
    bq_writer = _FakeBQRows()
    dedup = _FakeDedup()
    gmail_factory = _StubGmailFactory()

    audit_bq = _FakeAuditClient()
    audit = AuditLogClient(project_id="p", bq_client=audit_bq)

    composer = EveningReflectionComposer(
        prompt_template="Recipient: {{recipient_email}} :: {{voice_memos_block}}",
        llm=_FakeLLM(),
    )
    writer = EveningReflectionWriter(bq_client=bq_writer, project_id="p", dedup_client=dedup)
    agent = EveningReflectionAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        completed_tasks_reader=CompletedTasksTodayReader(bq_client=bq_rows, project_id="p"),
        triaged_today_reader=(
            triaged_reader
            if triaged_reader is not None
            else TriagedItemsTodayReader(bq_client=bq_rows, project_id="p")
        ),
        morning_brief_reader=MorningBriefForTodayReader(bq_client=bq_rows, project_id="p"),
        active_risk_flags_reader=ActiveRiskFlagsTodayReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=gmail_factory),
        writer=writer,
        voice_memos_reader=voice_memos_reader,
        in_flight_decisions_reader=in_flight_decisions_reader,
    )
    return agent, gmail_factory


def test_reflect_mode_calls_voice_memos_reader_and_not_in_flight():
    voice_reader = _CountingReader(
        [
            VoiceMemo(
                note_id="captures-recABC",
                markdown_content="A thought.",
                ingested_at=datetime(2026, 5, 5, 20, 30, tzinfo=UTC),
            )
        ]
    )
    in_flight_reader = _CountingReader([])
    agent, gmail_factory = _build_agent(
        voice_memos_reader=voice_reader,
        in_flight_decisions_reader=in_flight_reader,
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.REFLECT,
        )
    )
    assert voice_reader.call_count == 1
    assert in_flight_reader.call_count == 0
    # Voice memos surfaced as a section.
    assert "voice_memos" in out.sections_used
    # Subject prefix is the REFLECT-mode label.
    raw = gmail_factory.last_body["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "Evening Reflection" in decoded
    assert "Evening Anchor" not in decoded


def test_prompt_mode_calls_in_flight_reader_and_not_voice_memos():
    voice_reader = _CountingReader([])
    in_flight_reader = _CountingReader(
        [
            InFlightDecision(
                decision_id="dec-1",
                title="Renew Client A engagement",
                context="Q3 contract expires soon",
                status="pending",
                decided_at=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
            )
        ]
    )
    agent, gmail_factory = _build_agent(
        voice_memos_reader=voice_reader,
        in_flight_decisions_reader=in_flight_reader,
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.PROMPT,
        )
    )
    assert in_flight_reader.call_count == 1
    assert voice_reader.call_count == 0
    # In-flight decisions surfaced as a section.
    assert "in_flight_decisions" in out.sections_used
    # Triaged data, even when present in REFLECT mode, is rendered as
    # open_followups in PROMPT mode (see _sections_used in agent.py).
    assert "triaged_today" not in out.sections_used
    # Subject prefix is the PROMPT-mode label.
    raw = gmail_factory.last_body["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "Evening Anchor" in decoded
    assert "Evening Reflection" not in decoded


def test_prompt_mode_with_actionable_triaged_uses_open_followups_label():
    """In PROMPT mode, triaged items rendered through the open-followups
    block surface in sections_used as ``open_followups``, not
    ``triaged_today``."""
    voice_reader = _CountingReader([])
    in_flight_reader = _CountingReader([])

    # Stub the triaged reader to return one actionable item.
    class _TriagedStub:
        def load(self, recipient_email, local_date):
            from agency_brain.agents.evening_reflection.models import (
                TriagedTodaySnippet,
            )

            return [
                TriagedTodaySnippet(
                    item_id="i-1",
                    severity="high",
                    summary="ClientC invoice 30 days overdue",
                    source="gmail",
                    action_type="do_now",
                )
            ]

    agent, _ = _build_agent(
        voice_memos_reader=voice_reader,
        in_flight_decisions_reader=in_flight_reader,
        triaged_reader=_TriagedStub(),
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.PROMPT,
        )
    )
    assert "open_followups" in out.sections_used
    assert "triaged_today" not in out.sections_used


def test_reflect_mode_with_no_voice_memo_reader_skips_silently():
    """If main.py runs reflect mode without populating the voice memo
    reader (defensive default), the agent must not crash — voice_memos
    just doesn't appear in sections_used."""
    agent, _ = _build_agent(
        voice_memos_reader=None,
        in_flight_decisions_reader=None,
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.REFLECT,
        )
    )
    assert "voice_memos" not in out.sections_used
    # Default behavior preserved for v1 callers.
    assert out.gmail_draft_id == "r-draft-id"
