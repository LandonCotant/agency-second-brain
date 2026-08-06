"""Notes ingestor input + output dataclasses.

Originally ADR 0031 (Samsung Notes ingestor — PDF only). Extended by
ADR 0037 (PKM merge — IPARAG folder taxonomy + multi-MIME) and
ADR 0038 (embeddings + VECTOR_SEARCH).

Mirrors the columns of ``agent_outputs.notes`` declared in
``terraform/modules/agent_runtime/main.tf``. The writer adds the
bookkeeping columns (``ingested_at``, etc.) that aren't produced by the
extractor.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class NoteFolder(enum.StrEnum):
    """Which folder a file was found in. Drives the HIPAA aspect, the
    note_kind, and whether the note publishes to triage.

    Back-compat with ADR 0031 (DEFAULT, HIPAA) is preserved — those
    folders still ingest as before, just with the new metadata
    columns populated from the role mapping below.

    ADR 0037 §2: new folder roles for the IPARAG-adapted Drive layout
    under ``Brain/``.
    """

    # ADR 0031 — back-compat
    DEFAULT = "default"  # legacy Brain Inbox/Notes/ — Samsung Notes lane
    HIPAA = "hipaa"  # Brain/Inbox/HIPAA/ — HIPAA-isolated personal capture

    # ADR 0037 — new lanes
    INBOX_VOICE = "inbox_voice"  # Brain/Inbox/Voice/
    INBOX_QUICKNOTES = "inbox_quicknotes"  # Brain/Inbox/QuickNotes/
    INBOX_READING = "inbox_reading"  # Brain/Inbox/Reading/
    AREAS = "areas"  # Brain/Areas/
    RESOURCES = "resources"  # Brain/Resources/
    ARCHIVES = "archives"  # Brain/Archives/

    # ADR 0048 — the agency Shared Drive sweep.
    # SOLUTIONS_CLIENT: Solutions/05_CLIENTS/<client>/<allowlisted-subfolder>/
    # SOLUTIONS_INTERNAL: Solutions/{01_MGMT,02_FIN,03_OPS,04_SALES}/...
    SOLUTIONS_CLIENT = "solutions_client"
    SOLUTIONS_INTERNAL = "solutions_internal"


class NoteKind(enum.StrEnum):
    """Persisted on ``agent_outputs.notes.note_kind``. ADR 0037 §2."""

    INBOX = "inbox"  # actionable capture — publishes to triage
    AREA = "area"  # ongoing reference — does NOT publish
    RESOURCE = "resource"  # static templates / frameworks — does NOT publish
    ARCHIVE = "archive"  # cold storage — does NOT publish
    GALAXY = "galaxy"  # promoted atomic permanent note (column flip)


class Scope(enum.StrEnum):
    """Persisted on ``agent_outputs.notes.scope`` and
    ``agent_outputs.triaged_items.scope``. ADR 0037 §1."""

    AGENCY = "agency"
    PERSONAL = "personal"


class ExtractionMethod(enum.StrEnum):
    """How the Markdown was produced. Persisted on
    ``agent_outputs.notes.extraction_method``."""

    GEMINI_FLASH = "gemini-2.5-flash"  # PDF (ADR 0031)
    GEMINI_FLASH_AUDIO = "gemini-2.5-flash-audio"  # voice memo (ADR 0037)
    GEMINI_FLASH_DOC_EXPORT = "gemini-2.5-flash-doc-export"  # Google Doc → MD
    MARKDOWN_PASSTHROUGH = "markdown-passthrough"  # .md file — no LLM
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Folder-role → kind / scope / publish mapping (ADR 0037)
# ---------------------------------------------------------------------------

_INBOX_FOLDERS = {
    NoteFolder.DEFAULT,
    NoteFolder.HIPAA,
    NoteFolder.INBOX_VOICE,
    NoteFolder.INBOX_QUICKNOTES,
    NoteFolder.INBOX_READING,
}


_SOLUTIONS_FOLDERS = {
    NoteFolder.SOLUTIONS_CLIENT,
    NoteFolder.SOLUTIONS_INTERNAL,
}


def note_kind_for_folder(folder: NoteFolder) -> NoteKind:
    """Map a folder role to its `note_kind`. ADR 0037 §2; ADR 0048 §2 for Solutions."""
    if folder in _INBOX_FOLDERS:
        return NoteKind.INBOX
    if folder is NoteFolder.AREAS:
        return NoteKind.AREA
    if folder is NoteFolder.RESOURCES:
        return NoteKind.RESOURCE
    if folder is NoteFolder.ARCHIVES:
        return NoteKind.ARCHIVE
    if folder in _SOLUTIONS_FOLDERS:
        return NoteKind.AREA
    raise ValueError(f"unmapped folder role: {folder!r}")


def scope_for_folder(folder: NoteFolder) -> Scope:
    """Brain/ captures are personal (ADR 0037 §1); Solutions/ captures
    are agency (ADR 0048 §2)."""
    if folder in _SOLUTIONS_FOLDERS:
        return Scope.AGENCY
    return Scope.PERSONAL


def should_publish_to_triage(folder: NoteFolder) -> bool:
    """ADR 0037 §6 — only INBOX kind publishes; reference folders don't.

    Solutions folders are AREA kind by design — they're reference
    material for /ask, not actionable inbox items.
    """
    return note_kind_for_folder(folder) is NoteKind.INBOX


def is_hipaa_folder(folder: NoteFolder) -> bool:
    """ADR 0031 §3 + ADR 0037 §2 — only the HIPAA inbox is hipaa-isolated
    at the folder-role level. For Solutions client folders, HIPAA is
    filtered at discovery time per ADR 0048 §3 (client folder name →
    accounts.hipaa lookup), so individual files reaching the ingester
    are non-HIPAA by construction."""
    return folder is NoteFolder.HIPAA


def should_move_to_processed(folder: NoteFolder) -> bool:
    """Brain inbox files get moved to ``processed/`` after a successful
    BQ write so the next tick doesn't re-list them. Solutions files are
    reference material that stays where the user/library put it; the
    revision-keyed dedup in NotesWriter prevents re-ingestion already.

    ADR 0048 §4 — never move Solutions files."""
    return folder not in _SOLUTIONS_FOLDERS


@dataclass(frozen=True)
class DriveFile:
    """One file discovered in a watched folder.

    `modified_time` carries Drive ``modifiedTime`` (advances on every
    edit/re-share); `revision_id` is the head revision id at list time
    (stable per content version). The dedup writer keys on
    ``(file_id, revision_id)``.

    `mime_type` was added by ADR 0037 to drive multi-MIME extractor
    dispatch. Default empty for back-compat with ADR 0031 callers that
    only handle PDF.
    """

    file_id: str
    revision_id: str
    name: str
    modified_time: datetime
    web_view_link: str
    folder: NoteFolder
    mime_type: str = ""


@dataclass(frozen=True)
class ExtractionResult:
    """Output of the Vertex Gemini multimodal call (or the markdown
    passthrough path for ``.md`` / Google Doc inputs)."""

    markdown: str
    page_count: int
    method: ExtractionMethod
    confidence: float
    """Self-rated [0,1]. Defaulted to 0.0 on FAILED, ~1.0 on clean text."""
    notes: str | None = None
    """Free-form extractor diagnostic (e.g., 'partial OCR, page 3 illegible')."""


@dataclass(frozen=True)
class EmbeddingResult:
    """Output of the Vertex text-embedding-005 call. ADR 0038 §1."""

    vector: tuple[float, ...]
    """768-dim. Empty tuple when the embedder is skipped (empty markdown)."""
    model: str
    """Identifier of the model that produced it (e.g., 'text-embedding-005')."""
    content_hash: str
    """SHA-256 of the markdown that was embedded. Idempotency guard for
    re-embed checks (ADR 0038 §4)."""


@dataclass(frozen=True)
class NoteRow:
    """One row in ``agent_outputs.notes``."""

    note_id: str
    """Equals ``DriveFile.file_id``. Stable across edits."""
    revision_id: str
    ingested_at: datetime  # UTC
    created_at: datetime  # Drive modifiedTime; partition column
    source_drive_file_id: str
    source_drive_url: str
    filename: str
    markdown_content: str
    extraction_method: ExtractionMethod
    extraction_confidence: float
    page_count: int
    hipaa_isolated: bool
    extraction_notes: str | None = None
    triaged_item_id: str | None = None
    """Back-ref written by the Triage RE writer chain post-classification."""

    # ADR 0037 / 0038 — PKM merge fields. All NULLABLE on the BQ side
    # because BigQuery requires column-adds-to-existing-tables to be
    # nullable. Pre-ADR-0037 rows have these as NULL and are backfilled
    # via a one-shot UPDATE post-deploy.
    note_kind: NoteKind | None = None
    scope: Scope | None = None
    embedding_content_hash: str | None = None
    embedding: tuple[float, ...] = ()
    embedding_model: str | None = None
    embedding_generated_at: datetime | None = None

    def to_bq_row(self) -> dict:
        row: dict = {
            "note_id": self.note_id,
            "revision_id": self.revision_id,
            "ingested_at": self.ingested_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "source_drive_file_id": self.source_drive_file_id,
            "source_drive_url": self.source_drive_url,
            "filename": self.filename,
            "markdown_content": self.markdown_content,
            "extraction_method": self.extraction_method.value,
            "extraction_confidence": self.extraction_confidence,
            "extraction_notes": self.extraction_notes,
            "page_count": self.page_count,
            "hipaa_isolated": self.hipaa_isolated,
            "triaged_item_id": self.triaged_item_id,
        }
        # PKM fields — only emit when set so existing tests / pre-merge
        # callers don't get spurious NULLs in BQ rows.
        if self.note_kind is not None:
            row["note_kind"] = self.note_kind.value
        if self.scope is not None:
            row["scope"] = self.scope.value
        if self.embedding_content_hash is not None:
            row["embedding_content_hash"] = self.embedding_content_hash
        if self.embedding:
            row["embedding"] = list(self.embedding)
        if self.embedding_model is not None:
            row["embedding_model"] = self.embedding_model
        if self.embedding_generated_at is not None:
            row["embedding_generated_at"] = self.embedding_generated_at.isoformat()
        return row


@dataclass(frozen=True)
class IngestOutcome:
    """Per-file outcome the main loop records on the audit row.

    Mirrors the shape ``RemoteTriageAgent`` writes via BaseAgent —
    output is a JSON-encoded dict with the file id, decision, and any
    error.
    """

    file_id: str
    revision_id: str
    folder: NoteFolder
    ingested: bool
    """True when a fresh row landed; False when the writer dedup-skipped."""
    triage_published: bool
    extraction_method: ExtractionMethod
    page_count: int
    error: str | None = None


@dataclass
class IngestSummary:
    """End-of-tick summary returned from `main()`. Useful for tests.

    Mutable on purpose — accumulated by ``run_tick`` as it processes
    folders. Callers treat it as read-only after return.
    """

    folder_outcomes: list[IngestOutcome] = field(default_factory=list)
    listed: int = 0
    extracted: int = 0
    written: int = 0
    triage_published: int = 0
    dedup_skipped: int = 0
    failures: int = 0
