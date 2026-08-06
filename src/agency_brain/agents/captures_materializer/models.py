"""Captures materializer dataclasses (ADR 0039).

Inputs: one ``Capture`` per unsynced row in ``airtable_replica.captures``.
Outputs per Kind:

  ``note``      → row in ``agent_outputs.notes`` + Pub/Sub publish
  ``decision``  → row in ``agent_outputs.decisions``
  ``win``       → row in ``agent_outputs.wins``
  ``todo``      → Pub/Sub publish only

The dispatch result (``MaterializeOutcome``) carries enough info for the
audit row + the orchestration loop to decide whether to flip ``Synced``
and DELETE the source row.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class CaptureKind(enum.StrEnum):
    """Mirrors the Captures.Kind singleSelect (airtable/schema.json)."""

    NOTE = "note"
    DECISION = "decision"
    WIN = "win"
    TODO = "todo"


class CaptureScope(enum.StrEnum):
    """Mirrors the Captures.Scope Hint singleSelect."""

    PERSONAL = "personal"
    AGENCY = "agency"


@dataclass(frozen=True)
class Capture:
    """One unsynced row from ``airtable_replica.captures``.

    ``record_id`` is the Airtable rec id (replica system column
    ``_airtable_record_id``); used both as the Airtable mutation key
    AND as the deterministic dedup key for ``agent_outputs.notes``
    (``note_id = f"captures-{record_id}"`` per ADR 0039 §2).
    """

    record_id: str
    """Airtable record id (``recXXXX``)."""

    captured_at: datetime
    """Airtable createdTime — the canonical capture timestamp."""

    note_text: str
    """Body of the capture (markdown-passthrough for Kind=note)."""

    kind: CaptureKind
    scope: CaptureScope


@dataclass(frozen=True)
class MaterializeOutcome:
    """Per-row result the orchestrator records on the audit row.

    ``triage_published`` is True only when the dispatch published to
    ``asb-triage-input`` (note + todo paths). ``bq_written`` is True when
    a row landed in any ``agent_outputs.*`` table (note/decision/win).
    Both can be True for ``note`` (writes notes row AND publishes).
    """

    record_id: str
    kind: CaptureKind
    scope: CaptureScope
    bq_written: bool
    triage_published: bool
    target_table: str | None = None
    """``notes``/``decisions``/``wins``/``None`` (todo). For audit + tests."""
    error: str | None = None


@dataclass
class IngestSummary:
    """End-of-tick summary returned from ``run_tick``. Mutable on purpose
    so the orchestrator can accumulate as it processes each capture."""

    listed: int = 0
    materialized: int = 0
    triage_published: int = 0
    deleted: int = 0
    failures: int = 0
