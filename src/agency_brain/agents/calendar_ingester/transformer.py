"""CalendarEvent → NoteRow transformer.

Builds the markdown content the embedder + retriever see, plus the
``event_metadata`` STRUCT and the deterministic note_id.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from ..notes_ingestor.embedder import (
    DEFAULT_DIMS,
    Embedder,
    content_hash,
    embed_markdown,
)
from ..notes_ingestor.embedder import (
    DEFAULT_MODEL as DEFAULT_EMBED_MODEL,
)
from .models import CalendarEvent, EventMetadata, NoteRow

log = logging.getLogger("agency_brain.agents.calendar_ingester.transformer")

CALENDAR_NOTE_KIND = "calendar_event"
EXTRACTION_METHOD = "calendar-api-v3"


def transform_event(
    *,
    event: CalendarEvent,
    embedder: Embedder,
    embed_model: str = DEFAULT_EMBED_MODEL,
    scope: str = "agency",
    embed_failures_counter: list[int] | None = None,
) -> NoteRow | None:
    """Convert ``event`` → ``NoteRow``.

    Returns None if the event has no useful content (no summary AND no
    description AND no attendees). Empty embeddings are tolerated — the
    row writes with an empty embedding vector and the retriever's
    ``ARRAY_LENGTH(embedding) = 768`` filter naturally skips it.

    ``embed_failures_counter`` is an optional 1-element list that the
    caller increments to track embed failures across a run.
    """
    md = _render_markdown(event)
    if not md.strip() and not event.attendees:
        return None

    embedding: tuple[float, ...] = ()
    try:
        result = embed_markdown(markdown=md, embedder=embedder, model=embed_model)
        if result.vector and len(result.vector) == DEFAULT_DIMS:
            embedding = result.vector
    except Exception:
        if embed_failures_counter is not None:
            embed_failures_counter[0] += 1
        log.exception(
            "calendar_ingester.transformer.embed_failed event_id=%s",
            event.event_id,
        )

    note_id = _make_note_id(event)
    metadata = _make_metadata(event)
    revision_id = event.updated_at.isoformat() if event.updated_at else event.event_id
    # `created_at` mirrors Drive modifiedTime semantics on this table.
    # Calendar event's start time is the most meaningful "when did this
    # thing exist"; degenerate events with no start/updated_at get the
    # ingestion time as a last resort.
    created_at = event.start or event.updated_at or datetime.now(UTC)
    return NoteRow(
        note_id=note_id,
        filename=_filename_for(event),
        markdown_content=md,
        source_drive_url=event.html_link,
        scope=scope,
        note_kind=CALENDAR_NOTE_KIND,
        hipaa_isolated=False,
        external_id=event.event_id,
        revision_id=revision_id,
        source_drive_file_id=f"cal:{event.calendar_id}:{event.event_id}",
        created_at=created_at,
        embedding=embedding,
        embedding_model=embed_model,
        embedding_content_hash=content_hash(md),
        event_metadata=metadata,
        ingested_at=datetime.now(UTC),
        extraction_method=EXTRACTION_METHOD,
    )


def _render_markdown(event: CalendarEvent) -> str:
    """Compose the embedding/synthesis surface for a calendar event.

    Including attendees + location in the embedded text means
    VECTOR_SEARCH can answer "when did I last meet with X" queries
    (ADR 0046 §2 / verification 3a).
    """
    lines: list[str] = []
    if event.summary:
        lines.append(f"# {event.summary}")
        lines.append("")
    if event.start:
        lines.append(
            f"**When:** {event.start.isoformat()}"
            + (f" → {event.end.isoformat()}" if event.end else "")
        )
    if event.location:
        lines.append(f"**Location:** {event.location}")
    if event.organizer:
        lines.append(f"**Organizer:** {event.organizer}")
    if event.attendees:
        lines.append(f"**Attendees:** {', '.join(event.attendees)}")
    if lines and lines[-1] != "":
        lines.append("")
    if event.description:
        lines.append(event.description)
    return "\n".join(lines).strip()


def _make_metadata(event: CalendarEvent) -> EventMetadata:
    return EventMetadata(
        start_iso=event.start.isoformat() if event.start else None,
        end_iso=event.end.isoformat() if event.end else None,
        attendees=event.attendees,
        organizer=event.organizer,
        location=event.location,
        status=event.status,
    )


def _make_note_id(event: CalendarEvent) -> str:
    """Deterministic note_id derived from calendar_id + event_id.

    Matches the ``cal-{hash12}`` convention so notes_links rows pointing
    at a calendar event are visually distinct from Drive-derived notes."""
    raw = f"{event.calendar_id}/{event.event_id}".encode()
    return f"cal-{hashlib.sha256(raw).hexdigest()[:12]}"


def _filename_for(event: CalendarEvent) -> str:
    """A pseudo-filename used for citation rendering. Includes the
    event date so humans see the temporal anchor in the Surfacer's
    Sources card."""
    when = event.start.strftime("%Y-%m-%d") if event.start else "undated"
    summary = (event.summary or "(no title)").strip()[:80]
    return f"[{when}] {summary}"
