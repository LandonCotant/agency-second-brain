"""Audit row emitter for People Sync (ADR 0057).

Mirrors ``librarian/writer.py`` — one ``run_summary`` row per Cloud
Run Job execution. Per-file outcomes accumulate into the summary
counters; we don't emit per-file rows here because the volumes are
small (typically <100 contacts + <50 accounts per tick) and the
summary is sufficient for a 2-person tool's debugging surface.

If finer-grained observability is needed later, extend to emit
per-file rows the same shape as librarian's ``file_outcome``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from ...common.audit_log import AuditLogClient
from ...common.models import AuditEvent, HipaaGuardStatus
from .models import SyncSummary

log = logging.getLogger("agency_brain.agents.people_sync.writer")

AGENT_ID = "people-sync"


class PeopleSyncAuditWriter:
    """Emits ``agent_audit_log.events`` rows for ``asb-people-sync``."""

    def __init__(self, *, audit: AuditLogClient, sa_email: str) -> None:
        self._audit = audit
        self._sa_email = sa_email

    def emit_run_summary(
        self, *, run_id: str, summary: SyncSummary, latency_ms: int, error: str | None = None
    ) -> None:
        output = json.dumps(
            {
                "event_kind": "run_summary",
                "run_id": run_id,
                "accounts": {
                    "listed": summary.accounts_listed,
                    "created": summary.accounts_created,
                    "updated": summary.accounts_updated,
                    "unchanged": summary.accounts_unchanged,
                    "archived": summary.accounts_archived,
                    "hipaa_skipped": summary.accounts_hipaa_skipped,
                    "failed": summary.accounts_failed,
                },
                "contacts": {
                    "listed": summary.contacts_listed,
                    "created": summary.contacts_created,
                    "updated": summary.contacts_updated,
                    "unchanged": summary.contacts_unchanged,
                    "archived": summary.contacts_archived,
                    "hipaa_skipped": summary.contacts_hipaa_skipped,
                    "failed": summary.contacts_failed,
                },
            }
        )
        input_summary = json.dumps({"source": "scheduler", "run_id": run_id})
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            agent_id=AGENT_ID,
            sa_email=self._sa_email,
            latency_ms=latency_ms,
            hipaa_guard_status=HipaaGuardStatus.PASSED,
            success=error is None and summary.accounts_failed == 0 and summary.contacts_failed == 0,
            human_review_routed=False,
            input_summary=input_summary,
            output=output,
            error=error,
        )
        try:
            self._audit.emit(event)
        except Exception:
            log.exception("people_sync.writer.emit_failed event_id=%s", event.event_id)
