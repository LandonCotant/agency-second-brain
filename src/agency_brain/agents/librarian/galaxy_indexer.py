"""Galaxy indexer — drop-to-index sweep of ``Brain/05_GALAXY/`` (ADR 0054 §2).

Galaxy is the user's capture surface for promoted permanent / atomic
notes. The file IS the source of truth; the Librarian indexes its
presence into ``agent_outputs.notes`` with ``note_kind='galaxy'`` and
runs the semantic-neighbor linker so each Galaxy file picks up
``notes_links`` edges. **No move, no classifier call.**

Why this isn't the regular Drop flow:
  - The user has already decided the file belongs in Galaxy; classifying
    + moving would fight the user.
  - The user's own folder structure inside Galaxy is meaningful (e.g.
    ``concepts/``, ``mental-models/``); we shouldn't flatten it.

Why this isn't the Notes Ingestor flow:
  - Notes Ingestor only watches Brain inbox lanes (Voice / QuickNotes /
    Reading / etc.) — adding Galaxy there would conflate user-promoted
    permanent notes with transient inbox capture.
  - The Librarian already owns the linker + dedup-keyed write path
    against ``agent_outputs.notes``; reusing it keeps Galaxy edges in
    the same ``notes_links`` table as drop-classified neighbors.

Wired into ``main.py`` after the Drop/QuickNotes loop. Optional —
``BRAIN_GALAXY_FOLDER_ID`` unset disables the sweep silently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..notes_ingestor.models import NoteKind, Scope
from .ingestor import LibrarianIngestor
from .models import (
    DropFile,
    IngestOutcomeSummary,
    LibrarianOutcome,
    LinkerOutcome,
)

log = logging.getLogger("agency_brain.agents.librarian.galaxy_indexer")

_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

GALAXY_PARENT_FOLDER_ROLE = "galaxy"
"""``DropFile.parent_folder_role`` value for Galaxy files. Distinct from
``drop`` / ``quicknotes`` so downstream auditors can filter by role."""


class DriveClient(Protocol):
    """Drive surface the Galaxy indexer needs.

    Matches the public methods on ``main._DriveAdapter`` (the
    production wiring) — listing for the recursive walk + download/
    export for content extraction. A fake in tests provides the same
    three methods.
    """

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]: ...

    def download_file(self, file_id: str) -> bytes: ...

    def export_doc_as_markdown(self, file_id: str) -> bytes: ...


class _Extractor(Protocol):
    def extract(self, *, data: bytes, mime_type: str, file_name: str) -> Any: ...


class _LinkerProto(Protocol):
    def link_for_drive_file(
        self, *, drive_file_id: str, dossier_doc_id: str | None = ...
    ) -> LinkerOutcome: ...


class _AuditWriterProto(Protocol):
    def emit_file_outcome(
        self, *, run_id: str, outcome: LibrarianOutcome, latency_ms: int
    ) -> None: ...


@dataclass(frozen=True)
class GalaxySweepSummary:
    """Run-level Galaxy bookkeeping consumed by the run summary log + audit row."""

    listed: int = 0
    indexed: int = 0
    deduped: int = 0
    failed: int = 0
    neighbors_linked_total: int = 0


class GalaxyIndexer:
    """Sweeps ``BRAIN_GALAXY_FOLDER_ID`` recursively + indexes each file.

    Reuses the existing ``LibrarianIngestor`` (write + embed + dedup) and
    ``LibrarianLinker`` (semantic neighbor edges). The only new code is
    the recursive walk + the per-file dispatch.
    """

    def __init__(
        self,
        *,
        drive_client: DriveClient,
        extractor: _Extractor,
        ingestor: LibrarianIngestor,
        linker: _LinkerProto,
        audit_writer: _AuditWriterProto,
        max_depth: int = 3,
        page_size: int = 100,
    ) -> None:
        self._drive = drive_client
        self._extractor = extractor
        self._ingestor = ingestor
        self._linker = linker
        self._audit = audit_writer
        self._max_depth = max_depth
        self._page_size = page_size

    def sweep(self, *, run_id: str, galaxy_folder_id: str) -> GalaxySweepSummary:
        """Walk Galaxy and index every fresh file. Returns aggregate summary."""
        if not galaxy_folder_id:
            return GalaxySweepSummary()
        files = self._list_recursive(galaxy_folder_id)
        log.info("librarian.galaxy.listed count=%d", len(files))

        indexed = 0
        deduped = 0
        failed = 0
        neighbors_total = 0

        for f in files:
            t0 = time.perf_counter()
            outcome = self._index_one(drive_file_raw=f)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            self._audit.emit_file_outcome(run_id=run_id, outcome=outcome, latency_ms=latency_ms)
            if outcome.error is not None:
                failed += 1
                continue
            # ``deduped`` is true on dedup hit; ``written`` is true on
            # fresh write. Both count as listed-and-processed.
            if outcome.ingest.deduped:
                deduped += 1
            elif outcome.ingest.written:
                indexed += 1
            neighbors_total += outcome.linker.neighbors_linked

        return GalaxySweepSummary(
            listed=len(files),
            indexed=indexed,
            deduped=deduped,
            failed=failed,
            neighbors_linked_total=neighbors_total,
        )

    # ----------------------------------------------------------- walk

    def _list_recursive(self, root_id: str) -> list[dict]:
        """Flatten the Galaxy subtree into a list of file dicts.

        Folders are walked depth-first; files are accumulated. Mirrors
        ``LibrarianAreasIndex._walk`` cap on depth (default 3) so a
        runaway nested folder doesn't break the tick.
        """
        out: list[dict] = []
        self._walk(parent_id=root_id, parent_path="galaxy", depth=0, out=out)
        return out

    def _walk(
        self,
        *,
        parent_id: str,
        parent_path: str,
        depth: int,
        out: list[dict],
    ) -> None:
        if depth >= self._max_depth:
            return
        try:
            raw_rows = self._drive.list_files(
                folder_id=parent_id,
                page_size=self._page_size,
                fields=("files(id, name, mimeType, modifiedTime, webViewLink)"),
            )
        except Exception:
            log.exception("librarian.galaxy.list_failed parent=%s", parent_id)
            return
        for raw in raw_rows:
            mime = str(raw.get("mimeType") or "")
            name = str(raw.get("name") or "").strip()
            file_id = str(raw.get("id") or "")
            if not file_id or not name or name.startswith("."):
                continue
            if mime == _GOOGLE_FOLDER_MIME:
                self._walk(
                    parent_id=file_id,
                    parent_path=f"{parent_path}/{name}",
                    depth=depth + 1,
                    out=out,
                )
                continue
            out.append(
                {
                    "id": file_id,
                    "name": name,
                    "mimeType": mime,
                    "modifiedTime": raw.get("modifiedTime"),
                    "webViewLink": raw.get("webViewLink"),
                    "parent_id": parent_id,
                    "parent_path": parent_path,
                }
            )

    # ----------------------------------------------------------- per-file

    def _index_one(self, *, drive_file_raw: dict) -> LibrarianOutcome:
        """One Galaxy file → DropFile → ingest (galaxy kind) → link."""
        file_id = str(drive_file_raw.get("id") or "")
        file_name = str(drive_file_raw.get("name") or "")
        parent_path = str(drive_file_raw.get("parent_path") or "galaxy")
        try:
            drop_file = _drop_file_for_galaxy(drive_file_raw)
        except Exception as exc:
            log.exception("librarian.galaxy.drop_file_build_failed file=%s", file_id)
            return _failure_outcome(
                file_id=file_id, file_name=file_name, error=exc, to_path=parent_path
            )

        try:
            markdown = self._extract_markdown(drop_file=drop_file)
        except Exception as exc:
            log.exception("librarian.galaxy.extract_failed file=%s", file_id)
            return _failure_outcome(
                file_id=file_id, file_name=file_name, error=exc, to_path=parent_path
            )

        try:
            result = self._ingestor.ingest(
                drop_file=drop_file,
                dest_folder=None,
                markdown=markdown,
                kind_override=NoteKind.GALAXY,
                scope_override=Scope.PERSONAL,
            )
        except Exception as exc:
            log.exception("librarian.galaxy.ingest_failed file=%s", file_id)
            return _failure_outcome(
                file_id=file_id, file_name=file_name, error=exc, to_path=parent_path
            )

        ingest_summary = IngestOutcomeSummary(
            note_id=result.note_id,
            written=result.written,
            deduped=result.deduped,
            embedded=result.embedded,
            error=result.error,
        )

        # Linker only when there's a fresh row to link AGAINST. On dedup
        # hit, the existing row already has its neighbors from the prior
        # pass; re-running is a no-op (existing_pair_keys filters dupes)
        # but skipping saves a BQ query.
        link_outcome = LinkerOutcome()
        if result.written and result.note_id is not None:
            try:
                link_outcome = self._linker.link_for_drive_file(
                    drive_file_id=file_id,
                    dossier_doc_id=None,
                )
            except Exception:
                log.exception("librarian.galaxy.link_failed file=%s", file_id)

        return LibrarianOutcome(
            file_id=file_id,
            file_name=file_name,
            from_folder_role=GALAXY_PARENT_FOLDER_ROLE,
            # Galaxy files don't move; "to_folder_path" surfaces the
            # walked path inside Galaxy so audit rows are still
            # diagnostic.
            to_folder_path=parent_path,
            confidence=1.0,  # User placed it; no classifier confidence.
            moved=False,
            linker=link_outcome,
            ingest=ingest_summary,
        )

    def _extract_markdown(self, *, drop_file: DropFile) -> str:
        """Best-effort content extraction. Empty markdown is acceptable
        (ingestor short-circuits + returns written=False), but most
        Galaxy files are markdown / Google Docs so this almost always
        produces content."""
        # Local import keeps this module independent of main.py's
        # adapter wiring — the extractor here is the production
        # ``GeminiMultimodalExtractor`` (or a fake in tests).
        mime = (drop_file.mime_type or "").lower()
        try:
            if mime == "application/vnd.google-apps.document":
                blob = self._drive.export_doc_as_markdown(drop_file.file_id)
            else:
                blob = self._drive.download_file(drop_file.file_id)
        except Exception:
            log.exception(
                "librarian.galaxy.download_failed file=%s mime=%s",
                drop_file.file_id,
                mime,
            )
            return ""
        if not blob:
            return ""
        if mime in {"text/markdown", "text/plain"}:
            try:
                return blob.decode("utf-8", errors="replace")
            except Exception:
                log.exception("librarian.galaxy.decode_failed file=%s", drop_file.file_id)
                return ""
        try:
            result = self._extractor.extract(
                data=blob,
                mime_type=drop_file.mime_type or "",
                file_name=drop_file.name,
            )
        except Exception:
            log.exception(
                "librarian.galaxy.extract_failed file=%s mime=%s",
                drop_file.file_id,
                mime,
            )
            return ""
        return getattr(result, "markdown", None) or ""


# --------------------------------------------------------------- helpers


def _drop_file_for_galaxy(raw: dict) -> DropFile:
    modified_raw = raw.get("modifiedTime")
    modified_dt: datetime
    if isinstance(modified_raw, str) and modified_raw:
        # Drive emits ISO 8601 with Z suffix; datetime.fromisoformat needs
        # explicit +00:00 in 3.10. Normalize.
        s = modified_raw.replace("Z", "+00:00")
        try:
            modified_dt = datetime.fromisoformat(s)
        except ValueError:
            from datetime import UTC

            modified_dt = datetime.now(UTC)
    else:
        from datetime import UTC

        modified_dt = datetime.now(UTC)
    return DropFile(
        file_id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        mime_type=str(raw.get("mimeType") or ""),
        parent_folder_id=str(raw.get("parent_id") or ""),
        parent_folder_role=GALAXY_PARENT_FOLDER_ROLE,
        modified_time=modified_dt,
        web_view_link=(str(raw.get("webViewLink")) if raw.get("webViewLink") else None),
    )


def _failure_outcome(
    *, file_id: str, file_name: str, error: Exception, to_path: str
) -> LibrarianOutcome:
    return LibrarianOutcome(
        file_id=file_id,
        file_name=file_name,
        from_folder_role=GALAXY_PARENT_FOLDER_ROLE,
        to_folder_path=to_path,
        confidence=0.0,
        moved=False,
        linker=LinkerOutcome(),
        error=f"{type(error).__name__}: {error}",
    )
