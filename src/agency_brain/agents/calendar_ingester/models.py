"""Calendar ingester dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CalendarEvent:
    """Normalized Calendar v3 event, post-Calendar API decoding."""

    event_id: str
    calendar_id: str
    summary: str
    description: str
    start: datetime | None
    end: datetime | None
    attendees: tuple[str, ...]
    organizer: str
    location: str
    status: str  # "confirmed" | "tentative" | "cancelled"
    html_link: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class EventMetadata:
    """Mirrors the ``event_metadata`` STRUCT on ``agent_outputs.notes``."""

    start_iso: str | None
    end_iso: str | None
    attendees: tuple[str, ...]
    organizer: str
    location: str
    status: str

    def to_bq_struct(self) -> dict:
        return {
            "start": self.start_iso,
            "end": self.end_iso,
            "attendees": list(self.attendees),
            "organizer": self.organizer,
            "location": self.location,
            "status": self.status,
        }


@dataclass(frozen=True)
class NoteRow:
    """One row to MERGE into ``agent_outputs.notes``.

    Matches the existing notes schema (ADR 0037 / ADR 0038):
      - note_id, filename, markdown_content, source_drive_url, scope,
        note_kind, hipaa_isolated, embedding, embedding_model,
        embedding_content_hash
      - PLUS the new event_metadata STRUCT (ADR 0046 / Calendar
        ingester schema add).
      - external_id is set to the Google Calendar event id; the MERGE
        key.
    """

    note_id: str
    filename: str
    markdown_content: str
    source_drive_url: str  # Calendar event htmlLink (clickable)
    scope: str  # "agency" | "personal"
    note_kind: str  # always "calendar_event"
    hipaa_isolated: bool
    external_id: str
    # ``revision_id`` is REQUIRED on agent_outputs.notes. For calendar
    # events there's no Drive headRevisionId; the Google Calendar API
    # exposes an ``updated`` timestamp that bumps on every edit, so we
    # use its ISO-8601 string (falls back to ``event_id`` if missing).
    revision_id: str
    # ``source_drive_file_id`` is REQUIRED on agent_outputs.notes (the
    # cluster key for the table, sized for Drive ingest). Calendar
    # events aren't Drive files; synthesize ``cal:{calendar_id}:{event_id}``
    # so clustering still bucket-isolates per calendar without polluting
    # the Drive-file-id namespace.
    source_drive_file_id: str
    # ``created_at`` is REQUIRED (modifiedTime semantics on Drive). For
    # calendar events the event's start time is the most meaningful
    # "when did this thing exist" value; falls back to ``updated_at``
    # or the current time on degenerate events.
    created_at: datetime
    embedding: tuple[float, ...]
    embedding_model: str
    embedding_content_hash: str
    event_metadata: EventMetadata
    ingested_at: datetime
    # ``extraction_confidence`` is REQUIRED ([0,1]). Calendar API is a
    # structured pull, not an LLM extraction; 1.0 is the right value.
    extraction_confidence: float = 1.0
    # ``page_count`` is REQUIRED (INT64). Calendar events aren't
    # paginated documents; 1 = "one event".
    page_count: int = 1
    extraction_method: str = "calendar-api-v3"


@dataclass(frozen=True)
class IngestResult:
    """Per-tick stats."""

    total_events: int
    inserted: int
    updated: int
    unchanged: int
    skipped_hipaa: int
    embed_failures: int
    errors: int


@dataclass(frozen=True)
class IngestRunCheckpoint:
    """One row of ``agent_state.calendar_ingester_runs`` (sibling table to
    ``notes_ingestor_watermark``)."""

    run_id: str
    started_at: datetime
    ended_at: datetime
    lookback_start_iso: str
    lookback_end_iso: str
    result: IngestResult
    success: bool
    aspects: list[str] = field(default_factory=list)
