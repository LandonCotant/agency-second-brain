"""Brag Spotter input + output dataclasses (ADR 0043).

Source-row dataclasses model only the projection the composer prompt
needs — keep Vertex token counts low and prompt context cohesive.

The agent's `BragSpotterOutput` mirrors the columns the writer needs
to populate `agent_outputs.wins` plus dispatch bookkeeping (Chat card
status, Gmail draft id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------- source rows


@dataclass(frozen=True)
class TriagedItemRow:
    """Projection of `agent_outputs.triaged_items`."""

    item_id: str
    triaged_at: datetime
    severity: str
    reasoning: str
    source: str
    source_url: str | None
    positive_goal_achieving: str | None


@dataclass(frozen=True)
class RoutedEventRow:
    """Projection of `agent_outputs.routed_events`."""

    item_id: str
    channel: str
    routed_at: datetime


@dataclass(frozen=True)
class NoteRow:
    """Projection of `agent_outputs.notes`."""

    note_id: str
    ingested_at: datetime
    filename: str
    extraction_method: str
    markdown_content: str | None
    source_drive_url: str | None


@dataclass(frozen=True)
class DecisionRow:
    """Projection of `agent_outputs.decisions`."""

    decision_id: str
    decided_at: datetime
    title: str
    context: str | None
    choice: str
    status: str


@dataclass(frozen=True)
class ReflectionRow:
    """Projection of `agent_outputs.evening_reflections`."""

    reflection_id: str
    generated_at: datetime
    local_date: date
    body_markdown: str
    sections_used: tuple[str, ...]


@dataclass(frozen=True)
class ExistingWinRow:
    """Projection of `agent_outputs.wins` for the current week's pre-check."""

    win_id: str
    title: str
    source_kind: str
    source_id: str | None


# ---------------------------------------------------------------- LLM payload


VALID_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "triaged_item",
        "routed_event",
        "note",
        "decision",
        "reflection",
    }
)


@dataclass(frozen=True)
class WinCandidate:
    """One LLM-extracted win candidate.

    `source_kind` MUST be one of `VALID_SOURCE_KINDS`. The agent
    rejects candidates that fail this check during parse.
    """

    title: str
    summary: str | None
    source_kind: str
    source_id: str
    evidence_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class BragSpotterPayload:
    """Parsed LLM response shape (ADR 0043 §4)."""

    commentary: str
    candidates: tuple[WinCandidate, ...] = ()


# ---------------------------------------------------------------- agent I/O


@dataclass(frozen=True)
class BragSpotterInput:
    """One Brag Spotter invocation = one recipient on one ISO week.

    `run_date` is the local date the run fired on; `week_of` is the
    ISO Monday derived from it (per ADR 0040 §6 / ADR 0043 §5).
    """

    recipient_email: str
    run_date: date
    week_of: date
    aspects: list[str] = field(default_factory=list)
    """BaseAgent HIPAA pre-flight; v1 always empty (filtering happens
    upstream at the BQ readers per ADR 0043 §8)."""


@dataclass(frozen=True)
class BragSpotterOutput:
    """Composed weekly digest + dispatch outcome.

    Mirrors what `writer.write` needs to record alongside the
    `agent_outputs.wins` rows. `wins_written` and `wins_skipped` are
    the per-candidate dispatch tally for audit summarization.
    """

    digest_id: str
    recipient_email: str
    week_of: date
    body_markdown: str
    candidates: tuple[WinCandidate, ...]
    wins_written: int
    wins_skipped: int
    sources_seen: tuple[str, ...]
    chat_status: int | None = None
    gmail_draft_id: str | None = None
    confidence: float = 1.0
