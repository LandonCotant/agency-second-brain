"""Dataclasses for the commitment extractor (ADR 0069).

``ExtractedCommitment`` is one item the LLM returns per source note.
``CommitmentRow`` is the persisted ``agent_outputs.commitments`` row —
the LLM fields plus provenance, resolved account, and bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass

# Direction of a promise relative to the operator (the operator).
DIRECTION_MINE = "mine"  # the operator promised to do something
DIRECTION_THEIRS = "theirs"  # someone promised the operator something
VALID_DIRECTIONS = (DIRECTION_MINE, DIRECTION_THEIRS)

# Lifecycle status. Extractor only ever writes ``open``; ``done`` /
# ``cancelled`` are reserved for a future mark-status tool (ADR 0069 deferred).
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"


@dataclass(frozen=True)
class SourceNote:
    """A corpus row the extractor scans."""

    note_id: str
    note_kind: str
    markdown_content: str
    ingested_at: str  # ISO-8601, drives the watermark cursor


@dataclass(frozen=True)
class ExtractedCommitment:
    """One commitment as returned by the LLM (pre-persistence)."""

    direction: str  # "mine" | "theirs"
    commitment_text: str
    counterparty_email: str | None
    counterparty_name: str | None
    due_date: str | None  # ISO date string or None
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class CommitmentRow:
    """A persisted ``agent_outputs.commitments`` row."""

    commitment_id: str
    extracted_at: str
    source_note_id: str
    source_note_kind: str
    direction: str
    counterparty_email: str | None
    counterparty_name: str | None
    account_id: str | None
    commitment_text: str
    due_date: str | None
    status: str
    confidence: float
    reasoning: str
    agent_run_id: str
