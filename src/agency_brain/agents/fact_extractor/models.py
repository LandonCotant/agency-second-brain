"""Dataclasses for the fact extractor (ADR 0070).

``ExtractedFact`` is one item the LLM returns per source note.
``FactRow`` is the persisted append-only ``agent_outputs.facts`` row.
"""

from __future__ import annotations

from dataclasses import dataclass

ENTITY_ACCOUNT = "account"
ENTITY_CONTACT = "contact"


@dataclass(frozen=True)
class SourceNote:
    """A corpus row the extractor scans."""

    note_id: str
    note_kind: str
    markdown_content: str
    ingested_at: str  # ISO-8601, drives the watermark cursor
    note_date: str | None  # ISO date — best-effort observed_date fallback


@dataclass(frozen=True)
class ExtractedFact:
    """One entity-attribute fact as returned by the LLM (pre-persistence)."""

    entity_name: str
    entity_email: str | None
    predicate: str  # normalized snake_case attribute key
    value: str
    observed_date: str | None  # ISO date or None
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class FactRow:
    """A persisted ``agent_outputs.facts`` row (append-only event log)."""

    fact_id: str
    extracted_at: str
    observed_date: str  # event time / valid_from
    entity_id: str | None
    entity_type: str | None
    entity_name: str
    predicate: str
    value: str
    source_note_id: str
    source_note_kind: str
    confidence: float
    agent_run_id: str
