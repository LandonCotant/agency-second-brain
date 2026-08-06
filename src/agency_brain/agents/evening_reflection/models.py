"""WS-G4 Evening Reflection input + output dataclasses (ADRs 0036, 0040).

Inputs are constructed by the Cloud Run Job entrypoint (one per recipient
per execution). Outputs mirror the columns of
``agent_outputs.evening_reflections`` declared in
``terraform/modules/agent_runtime/main.tf``. The writer in ``writer.py``
adds ``reflection_id``, ``agent_run_id``, ``model``, ``latency_ms``,
``success``, and ``error`` — those are not produced by the LLM.

Per ADR 0036 §1: drafts-only (PRD §4.7); never auto-sent.

ADR 0040 (PKM Phase 1) adds a ``ReflectionMode`` enum + two reader-result
dataclasses (``VoiceMemo``, ``InFlightDecision``) for the new prompt /
reflect dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class ReflectionMode(Enum):
    """Which evening-reflection stance is running this tick (ADR 0040 §1).

    PROMPT: 16:00 PT forward-looking anchor. Reads in-flight decisions +
        actionable open followups + calendar; drafts a Gmail nudge with
        1-3 questions for the last hours of the day.

    REFLECT: 21:00 PT backward-looking reflection. Reads the v1 five
        sources plus today's voice memos (24h rolling per ADR 0040 §2);
        drafts the prose body. PR-B layers structured extraction over
        this mode (decisions/wins INSERTs + todo Pub/Sub publish).
    """

    PROMPT = "prompt"
    REFLECT = "reflect"


@dataclass(frozen=True)
class CompletedTaskSnippet:
    """An Airtable Task with status='Done' and completed_date=today."""

    task_id: str
    name: str
    project_name: str | None = None
    completed_date: date | None = None


@dataclass(frozen=True)
class TriagedTodaySnippet:
    """A Triaged Items row from today (any severity, ADR 0036 §6)."""

    item_id: str
    severity: str
    summary: str
    source: str
    action_type: str
    source_url: str | None = None


@dataclass(frozen=True)
class MorningBriefPlanSnippet:
    """Today's morning brief, surfaced for plan-vs-execution comparison."""

    brief_id: str
    body_markdown: str
    sections_used: tuple[str, ...]


@dataclass(frozen=True)
class ActiveRiskFlagSnippet:
    """Risk Watcher flag flagged today and not yet resolved."""

    flag_id: str
    severity: str
    pattern_name: str
    account_name: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class VoiceMemo:
    """A transcribed voice memo from ``agent_outputs.notes`` (ADR 0040 §2/§3).

    ``ingested_at`` is UTC. The 24h rolling window in
    ``RecentVoiceMemosReader`` filters on this column. ``markdown_content``
    is the Gemini-multimodal transcription written by Notes Ingestor.
    """

    note_id: str
    markdown_content: str
    ingested_at: datetime


@dataclass(frozen=True)
class InFlightDecision:
    """An in-flight ``agent_outputs.decisions`` row (ADR 0040 §4).

    Surfaced in PROMPT mode as the active-goals proxy: decisions in
    ``status IN ('draft', 'pending')`` from the last 30 days. ``decided_at``
    is UTC.
    """

    decision_id: str
    title: str
    context: str | None
    status: str
    decided_at: datetime


@dataclass(frozen=True)
class ExtractedDecision:
    """A decision extracted from the day's voice memos by REFLECT mode (ADR 0040 §5).

    Maps onto ``agent_outputs.decisions`` columns. ``source_voice_note_id``
    is populated when the model can attribute the decision to a specific
    voice memo; left ``None`` for synthesized decisions that draw from
    multiple sources or non-memo context.
    """

    title: str
    context: str | None = None
    source_voice_note_id: str | None = None


@dataclass(frozen=True)
class ExtractedWin:
    """A win extracted from the day's voice memos by REFLECT mode (ADR 0040 §5).

    Maps onto ``agent_outputs.wins`` columns; ``source_kind`` is set to
    ``reflection`` by the writer (not the LLM).
    """

    title: str
    summary: str | None = None
    source_voice_note_id: str | None = None


@dataclass(frozen=True)
class ExtractedTodo:
    """A todo extracted from the day's voice memos by REFLECT mode (ADR 0040 §5).

    Todos publish directly to ``asb-triage-input`` Pub/Sub (no
    ``agent_outputs.todos`` table — Triage Agent produces its own
    ``triaged_items`` row from the envelope).
    """

    body: str
    source_voice_note_id: str | None = None


@dataclass(frozen=True)
class ReflectExtractionPayload:
    """Parsed Gemini ``response_schema`` output for REFLECT mode.

    The composer's ``compose_structured`` (and ``compose_doc``) returns
    this. ``commentary`` is the prose body. The three structured arrays
    drive BQ writes / Pub/Sub publishes via ``reflect_dispatch.dispatch``.
    ``custom_questions`` are 2-3 day-specific reflection prompts the
    Doc body renders alongside the standard set (ADR 0044).
    """

    commentary: str
    decisions: tuple[ExtractedDecision, ...] = ()
    wins: tuple[ExtractedWin, ...] = ()
    todos: tuple[ExtractedTodo, ...] = ()
    custom_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class EveningReflectionInput:
    """One reflection invocation = one recipient on one local date."""

    recipient_email: str
    run_date: date
    """Calendar date the reflection covers (in REFLECTION_TIMEZONE)."""
    aspects: list[str] = field(default_factory=list)
    """Knowledge Catalog aspects. Triggers BaseAgent HIPAA pre-flight when
    ``hipaa_excluded`` is present. Empty for v1."""
    mode: ReflectionMode = ReflectionMode.REFLECT
    """ADR 0040 §1 — selects PROMPT vs REFLECT dispatch in
    ``EveningReflectionAgent._run``. Defaults to REFLECT so v1 callers
    that don't pass this field continue to behave exactly as ADR 0036
    specified until PR-C wires the second scheduler."""


@dataclass(frozen=True)
class EveningReflectionOutput:
    """Composed reflection + dispatch outcome.

    Mirrors ``agent_outputs.evening_reflections`` columns. The writer
    adds the bookkeeping columns that aren't on this dataclass.
    """

    reflection_id: str
    recipient_email: str
    local_date: date
    body_markdown: str
    sections_used: tuple[str, ...]
    prompt_version: str
    confidence: float = 1.0
    """BaseAgent requires a confidence in [0.0, 1.0]. The reflection is
    composed by an LLM but the rendering is deterministic; confidence
    here reflects whether all data sources returned cleanly. Default
    1.0 — readers that fail set this to 0.6 to route to human review."""
    gmail_draft_id: str | None = None
    dedup_skipped: bool = False
    dedup_existing_reflection_id: str | None = None
    reflection_doc_id: str | None = None
    """ADR 0044 — REFLECT-mode artifact. Populated when the Reflection
    Doc was created in ``Brain/Areas/Reflections/``. PROMPT mode leaves
    this None (PROMPT keeps the Gmail-draft surface)."""
    reflection_doc_url: str | None = None
    """Drive ``webViewLink`` for the Reflection Doc. The Chat-card
    notification embeds this URL."""


# CalendarEvent is reused directly from morning_brief.models — no need
# to redeclare. Importers that want it should:
#   from ..morning_brief.models import CalendarEvent
