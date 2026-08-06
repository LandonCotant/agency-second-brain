"""Librarian-as-ingestor (Phase G).

After the Librarian moves a file into a destination folder, this module
writes the file's content into ``agent_outputs.notes`` so it becomes
brain corpus — searchable via VECTOR_SEARCH, surfaceable in Reflection's
Areas context (Phase C), and eligible as a neighbor for the linker.

Without this step, files routed to client folders (which the Notes
Ingestor doesn't watch) would never become embedded corpus and the
wiki-linking goal would silently fail.

Mirrors the Notes Ingestor's embed-on-write via ``text-embedding-005``
and ``NoteRow`` shape, but its write is keyed on ``note_id`` (== Drive
``file_id``): a re-ingest of a changed file MERGE-UPDATEs the existing
row rather than appending a new revision row each sweep (ADR 0071 — the
galaxy/people accumulation fix; ``calendar_event`` already MERGEs). The
other differences:
  - ``note_kind`` and ``scope`` derive from the destination root_label
    (e.g. ``clients`` → ``area`` + ``agency``; ``brain`` → ``area``
    + ``personal``).
  - ``hipaa_isolated`` is always ``False`` — the Librarian's lister
    refuses to even see HIPAA folders, and HIPAA-bearing client
    material must be filed via a separate path.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..notes_ingestor.embedder import (
    DEFAULT_MODEL as DEFAULT_EMBED_MODEL,
)
from ..notes_ingestor.embedder import Embedder, embed_markdown
from ..notes_ingestor.models import (
    ExtractionMethod,
    NoteKind,
    NoteRow,
    Scope,
)
from ..notes_ingestor.writer import NotesWriter
from .models import AreaFolder, DropFile

log = logging.getLogger("agency_brain.agents.librarian.ingestor")


@dataclass(frozen=True)
class IngestOutcome:
    """What the ingestor did for one file. Surfaces in audit rows."""

    note_id: str | None
    """``agent_outputs.notes.note_id`` for the freshly-written or deduped
    row; None when ingestion was skipped (e.g. empty content)."""
    written: bool
    """True when a new row was inserted; False on dedup hit or skip."""
    deduped: bool = False
    embedded: bool = False
    error: str | None = None


class DriveMetaClient(Protocol):
    """Minimal Drive surface to fetch ``revision_id`` + ``modifiedTime``
    after the move (the move call itself doesn't return them).
    """

    def get_file_meta(self, file_id: str) -> dict: ...


class LibrarianIngestor:
    """Writes a moved file's content into ``agent_outputs.notes``."""

    def __init__(
        self,
        *,
        notes_writer: NotesWriter,
        embedder: Embedder,
        drive_meta: DriveMetaClient,
        embedding_model: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self._writer = notes_writer
        self._embedder = embedder
        self._drive = drive_meta
        self._embed_model = embedding_model

    def ingest(
        self,
        *,
        drop_file: DropFile,
        dest_folder: AreaFolder | None,
        markdown: str,
        extraction_method: ExtractionMethod = ExtractionMethod.MARKDOWN_PASSTHROUGH,
        page_count: int = 0,
        extraction_confidence: float = 1.0,
        kind_override: NoteKind | None = None,
        scope_override: Scope | None = None,
    ) -> IngestOutcome:
        """Write the moved file as a row in ``agent_outputs.notes``.

        ``dest_folder`` carries the destination root's label which drives
        ``note_kind`` + ``scope``. None (uncategorized fallback) defaults
        to ``area`` + ``agency`` so the row is still searchable but
        clearly flagged as un-routed.

        ``kind_override`` / ``scope_override`` let non-classifier callers
        (e.g. the Galaxy indexer, ADR 0054 §2) pin the row's kind/scope
        independent of bucket inference. Galaxy passes
        ``kind_override=NoteKind.GALAXY, scope_override=Scope.PERSONAL,
        dest_folder=None``.
        """
        if not (markdown or "").strip():
            log.info("librarian.ingest.skip_empty_content file=%s", drop_file.file_id)
            return IngestOutcome(note_id=None, written=False)

        try:
            meta = self._drive.get_file_meta(drop_file.file_id)
        except Exception as exc:
            log.exception("librarian.ingest.meta_fetch_failed file=%s", drop_file.file_id)
            return IngestOutcome(
                note_id=None,
                written=False,
                error=f"meta_fetch: {type(exc).__name__}: {exc}",
            )

        revision_id = str(meta.get("headRevisionId") or meta.get("revisionId") or "").strip()
        if not revision_id:
            # Google Docs / Sheets in Shared Drives sometimes don't expose
            # headRevisionId directly. Key on a content hash rather than
            # modifiedTime: modifiedTime advances on metadata-only changes
            # (rename, re-share, comment), which would defeat find_existing
            # and write a duplicate corpus row for unchanged content. A
            # SHA-256 of the markdown is stable across those non-edits.
            content_hash = hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()
            revision_id = f"sha256:{content_hash}"
        modified_time = drop_file.modified_time

        # ADR 0071 — idempotency keys on note_id (== file_id, stable across
        # edits), not (file_id, revision_id). A matching revision means the
        # content is unchanged → skip embed + skip the MERGE. A changed
        # revision falls through to embed + merge_by_note_id, which UPDATEs
        # the existing row in place instead of appending a new revision row
        # each sweep (the galaxy/people accumulation bug).
        prior_revision = self._writer.find_revision_by_note_id(note_id=drop_file.file_id)
        if prior_revision is not None and prior_revision == revision_id:
            log.info(
                "librarian.ingest.dedup_hit file=%s revision=%s",
                drop_file.file_id,
                revision_id,
            )
            return IngestOutcome(note_id=drop_file.file_id, written=False, deduped=True)

        # Embed inline. embed_markdown short-circuits empty markdown to
        # an empty vector + raises on backend errors; we tolerate the
        # exception and write the row without an embedding so the user
        # still gets the corpus row (filename / markdown_content) and a
        # follow-up backfill can fill embedding=NULL rows.
        embedding_result = None
        embedded = False
        try:
            embedding_result = embed_markdown(
                markdown=markdown,
                embedder=self._embedder,
                model=self._embed_model,
            )
            embedded = bool(embedding_result.vector)
        except Exception:
            log.exception("librarian.ingest.embed_failed file=%s", drop_file.file_id)

        dest_bucket = dest_folder.bucket if dest_folder else ""
        dest_label = dest_folder.root_label if dest_folder else ""
        kind = kind_override if kind_override is not None else _kind_for_bucket(dest_bucket)
        scope = (
            scope_override
            if scope_override is not None
            else _scope_for_root_label(dest_label, bucket=dest_bucket)
        )

        now = datetime.now(UTC)
        row = NoteRow(
            note_id=drop_file.file_id,
            revision_id=revision_id,
            ingested_at=now,
            created_at=modified_time,
            source_drive_file_id=drop_file.file_id,
            source_drive_url=drop_file.web_view_link or _doc_url(drop_file.file_id),
            filename=drop_file.name,
            markdown_content=markdown,
            extraction_method=extraction_method,
            extraction_confidence=extraction_confidence,
            page_count=page_count,
            hipaa_isolated=False,  # Librarian never touches HIPAA folders
            extraction_notes=_extraction_notes(
                dest_folder=dest_folder, kind_override=kind_override
            ),
            note_kind=kind,
            scope=scope,
            embedding_content_hash=(embedding_result.content_hash if embedding_result else None),
            embedding=(embedding_result.vector if embedding_result else ()),
            embedding_model=(embedding_result.model if (embedding_result and embedded) else None),
            embedding_generated_at=(now if embedded else None),
        )
        try:
            self._writer.merge_by_note_id(row)
        except Exception as exc:
            log.exception("librarian.ingest.write_failed file=%s", drop_file.file_id)
            return IngestOutcome(
                note_id=None,
                written=False,
                error=f"write: {type(exc).__name__}: {exc}",
            )

        log.info(
            "librarian.ingest.wrote note_id=%s embedded=%s kind=%s scope=%s",
            row.note_id,
            embedded,
            kind.value if kind else None,
            scope.value if scope else None,
        )
        return IngestOutcome(
            note_id=row.note_id,
            written=True,
            embedded=embedded,
        )


# --------------------------------------------------------------- helpers


def _extraction_notes(*, dest_folder: AreaFolder | None, kind_override: NoteKind | None) -> str:
    """Tag the row's ``extraction_notes`` with the source of the write.

    Galaxy (kind_override=GALAXY, dest_folder=None) gets a distinct tag
    so audit queries can separate Galaxy indexing from drop-classified
    uncategorized fallback.
    """
    if dest_folder is not None:
        return f"librarian: dest={dest_folder.path}"
    if kind_override is NoteKind.GALAXY:
        return "librarian: galaxy_index"
    return "librarian: uncategorized"


def _kind_for_bucket(bucket: str) -> NoteKind:
    """Map an ``AreaFolder.bucket`` to a ``NoteKind``. ADR 0054.

    ``resources`` → ``resource`` (templates, frameworks, prompts; reusable
    across projects). Everything else (``areas``, ``clients``, etc.) →
    ``area`` (ongoing reference / the work itself). Galaxy uses
    ``kind_override`` in the ingestor caller rather than a bucket, so
    we don't surface it here.
    """
    if (bucket or "").strip().lower() == "resources":
        return NoteKind.RESOURCE
    return NoteKind.AREA


def _scope_for_root_label(label: str, *, bucket: str = "") -> Scope:
    """Map a destination root label (+ bucket) to a ``Scope``.

    Convention: personal Brain content (``brain`` / ``personal`` roots,
    AND the personal Resources bucket since Resources lives in Brain)
    is ``personal`` scope; everything else (Solutions ``clients`` root,
    ops, etc.) is ``agency``.
    """
    label_lc = (label or "").lower()
    bucket_lc = (bucket or "").lower()
    if label_lc in {"brain", "personal", "personal_brain", "resources"}:
        return Scope.PERSONAL
    if bucket_lc == "resources":
        # Resources roots are personal regardless of how they were labeled.
        return Scope.PERSONAL
    return Scope.AGENCY


def _doc_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


# --------------------------------------------------------------- production


class DriveMetaAdapter:
    """Minimal ``DriveMetaClient`` over a googleapiclient Drive service.

    Wraps ``files.get(fileId, fields=...)`` returning the raw dict.
    """

    def __init__(self, service) -> None:
        self._svc = service

    def get_file_meta(self, file_id: str) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return (
                self._svc.files()
                .get(
                    fileId=file_id,
                    fields="id, name, mimeType, headRevisionId, modifiedTime, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError:
            log.exception("librarian.ingest.drive_get_failed file=%s", file_id)
            raise
