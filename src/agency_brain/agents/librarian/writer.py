"""Audit-row emitter for the Librarian (ADR 0044).

Mirrors ``notes_ingestor.main._emit_audit`` — single
``agent_audit_log.events`` row per file processed, plus a summary row
at the end of the tick.

Per-file rows carry the move outcome (from_folder, to_folder, confidence)
AND the linker bookkeeping (neighbors_linked, related_section_updated)
so a single audit query tells the full story for each file.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from ...common.audit_log import AuditLogClient
from ...common.models import AuditEvent, HipaaGuardStatus
from .models import LibrarianOutcome, LibrarianSummary

log = logging.getLogger("agency_brain.agents.librarian.writer")

AGENT_ID = "librarian"


class LibrarianAuditWriter:
    """Emits ``agent_audit_log.events`` rows for the Librarian.

    Audit failures must not silently drop, but they also must not cause
    us to lose the move/link bookkeeping that already happened. We log
    loudly and move on — PRD §8.2's audit-write-failure alert surfaces
    these in monitoring.
    """

    def __init__(
        self,
        *,
        audit: AuditLogClient,
        sa_email: str,
    ) -> None:
        self._audit = audit
        self._sa_email = sa_email

    def emit_file_outcome(self, *, run_id: str, outcome: LibrarianOutcome, latency_ms: int) -> None:
        success = outcome.error is None
        # ADR 0054 §2 — Galaxy files surface as a distinct event_kind so
        # audit queries can separate drop-classification work from
        # Galaxy indexing without re-deriving role from path.
        event_kind = "galaxy_index" if outcome.from_folder_role == "galaxy" else "file_outcome"
        output = json.dumps(
            {
                "event_kind": event_kind,
                "run_id": run_id,
                "file_id": outcome.file_id,
                "file_name": outcome.file_name,
                "from_folder_role": outcome.from_folder_role,
                "to_folder_path": outcome.to_folder_path,
                "confidence": outcome.confidence,
                "moved": outcome.moved,
                "neighbors_linked": outcome.linker.neighbors_linked,
                "related_section_updated": outcome.linker.related_section_updated,
                "dossier_doc_id": outcome.linker.dossier_doc_id,
                "ingest_note_id": outcome.ingest.note_id,
                "ingest_written": outcome.ingest.written,
                "ingest_deduped": outcome.ingest.deduped,
                "ingest_embedded": outcome.ingest.embedded,
                "ingest_error": outcome.ingest.error,
            }
        )
        input_summary = json.dumps(
            {
                "source": "drive",
                "file_id": outcome.file_id,
                "file_name": outcome.file_name,
                "from_folder_role": outcome.from_folder_role,
            }
        )
        self._emit(
            event_id=str(uuid.uuid4()),
            success=success,
            error=outcome.error,
            input_summary=input_summary,
            output=output,
            latency_ms=latency_ms,
        )

    def emit_run_summary(self, *, run_id: str, summary: LibrarianSummary, latency_ms: int) -> None:
        output = json.dumps(
            {
                "event_kind": "run_summary",
                "run_id": run_id,
                "listed": summary.listed,
                "moved": summary.moved,
                "uncategorized": summary.uncategorized,
                "failed": summary.failed,
                "neighbors_linked_total": summary.neighbors_linked_total,
                "related_sections_updated": summary.related_sections_updated,
                # ADR 0054 §2 — Galaxy sweep aggregates.
                "galaxy_listed": summary.galaxy_listed,
                "galaxy_indexed": summary.galaxy_indexed,
                "galaxy_deduped": summary.galaxy_deduped,
                "galaxy_failed": summary.galaxy_failed,
            }
        )
        input_summary = json.dumps({"source": "scheduler", "run_id": run_id})
        self._emit(
            event_id=str(uuid.uuid4()),
            success=True,
            error=None,
            input_summary=input_summary,
            output=output,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------ helpers

    def _emit(
        self,
        *,
        event_id: str,
        success: bool,
        error: str | None,
        input_summary: str,
        output: str,
        latency_ms: int,
    ) -> None:
        event = AuditEvent(
            event_id=event_id,
            timestamp=datetime.now(UTC),
            agent_id=AGENT_ID,
            sa_email=self._sa_email,
            latency_ms=latency_ms,
            hipaa_guard_status=HipaaGuardStatus.PASSED,
            success=success,
            human_review_routed=False,
            input_summary=input_summary,
            output=output,
            error=error,
        )
        try:
            self._audit.emit(event)
        except Exception:
            log.exception("librarian.writer.emit_failed event_id=%s", event_id)
