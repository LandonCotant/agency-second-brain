"""Captures Materializer agent (ADR 0039).

One agent instance per Cloud Run Job execution. ``materialize`` is the
unit of work — for each unsynced Capture row, dispatch by Kind, flip
``Synced`` in Airtable, then DELETE the source row.

This is NOT a ``BaseAgent`` subclass. The unit of work for a BaseAgent
subclass is one agent invocation == one classification == one BQ row.
The materializer's tick processes N rows and emits N audit events; the
notes-ingestor uses the same shape (see
``src/agency_brain/agents/notes_ingestor/main.py`` ``_emit_audit``)
and explicitly opts out of BaseAgent for that reason. Audit emission
mirrors BaseAgent's shape directly.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

from ...common.audit_log import AuditLogClient
from ...common.models import AuditEvent, HipaaGuardStatus
from .airtable_writer import AirtableWriteError, CapturesAirtableWriter
from .dispatch import (
    BQQueryClient,
    BQRowsClient,
    DispatchConfig,
    Embedder,
    PubSubPublisherClient,
    dispatch,
)
from .models import Capture, IngestSummary, MaterializeOutcome

log = logging.getLogger("agency_brain.agents.captures_materializer.agent")

AGENT_ID = "captures-materializer"


class CapturesMaterializerAgent:
    """Materialize one Capture row into the canonical store(s) + clean up."""

    def __init__(
        self,
        *,
        sa_email: str,
        audit_log: AuditLogClient,
        airtable_writer: CapturesAirtableWriter,
        config: DispatchConfig,
        bq: BQRowsClient,
        bq_query: BQQueryClient,
        publisher: PubSubPublisherClient,
        embedder: Embedder,
    ) -> None:
        self._sa_email = sa_email
        self._audit_log = audit_log
        self._airtable_writer = airtable_writer
        self._config = config
        self._bq = bq
        self._bq_query = bq_query
        self._publisher = publisher
        self._embedder = embedder

    # ------------------------------------------------------------------ public

    def invoke(self, capture: Capture) -> MaterializeOutcome:
        """Materialize one ``Capture``.

        Returns the dispatch outcome. The Airtable flip + DELETE happen
        only when the dispatch succeeded (``error is None``). A flip or
        DELETE failure is recorded on the outcome's ``error`` so the
        next tick retries; ADR 0039 §3 covers the failure modes.

        Always emits one audit row, success or failure.
        """
        started = time.perf_counter()
        outcome = dispatch(
            capture,
            config=self._config,
            bq=self._bq,
            bq_query=self._bq_query,
            publisher=self._publisher,
            embedder=self._embedder,
        )

        deleted = False
        flip_synced = False
        if outcome.error is None:
            try:
                self._airtable_writer.flip_synced(capture.record_id)
                flip_synced = True
            except AirtableWriteError as exc:
                log.exception(
                    "captures_materializer.flip_synced_failed record_id=%s",
                    capture.record_id,
                )
                outcome = _augment_error(outcome, str(exc))

            if flip_synced:
                try:
                    self._airtable_writer.delete_capture(capture.record_id)
                    deleted = True
                except AirtableWriteError as exc:
                    log.exception(
                        "captures_materializer.delete_failed record_id=%s",
                        capture.record_id,
                    )
                    # DELETE failure is non-fatal — flip succeeded, so the
                    # next tick filters this row out via the readers'
                    # ``synced=FALSE`` clause. Still surface on the outcome
                    # for the audit row.
                    outcome = _augment_error(outcome, str(exc))

        self._emit_audit(
            capture=capture,
            outcome=outcome,
            flip_synced=flip_synced,
            deleted=deleted,
            started_perf=started,
        )
        return outcome

    # ---------------------------------------------------------------- audit

    def _emit_audit(
        self,
        *,
        capture: Capture,
        outcome: MaterializeOutcome,
        flip_synced: bool,
        deleted: bool,
        started_perf: float,
    ) -> None:
        latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
        input_summary = json.dumps(
            {
                "record_id": capture.record_id,
                "kind": capture.kind.value,
                "scope": capture.scope.value,
                "captured_at": capture.captured_at.isoformat(),
                "body_chars": len(capture.note_text or ""),
            }
        )
        output_summary = json.dumps(
            {
                "kind": outcome.kind.value,
                "scope": outcome.scope.value,
                "bq_written": outcome.bq_written,
                "triage_published": outcome.triage_published,
                "target_table": outcome.target_table,
                "flip_synced": flip_synced,
                "deleted": deleted,
            }
        )
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            agent_id=AGENT_ID,
            sa_email=self._sa_email,
            latency_ms=latency_ms,
            # Captures has no HIPAA path (ADR 0039 §6) — non-HIPAA by
            # design. PASSED here means "no HIPAA-isolation invariant
            # was violated by this materialize."
            hipaa_guard_status=HipaaGuardStatus.PASSED,
            success=outcome.error is None,
            human_review_routed=False,
            input_summary=input_summary,
            output=output_summary,
            error=outcome.error,
        )
        try:
            self._audit_log.emit(event)
        except Exception:
            log.exception(
                "captures_materializer.audit_emit_failed event_id=%s",
                event.event_id,
            )


def run_tick(
    captures: list[Capture],
    *,
    agent: CapturesMaterializerAgent,
) -> IngestSummary:
    """Iterate ``captures`` through ``agent.invoke``; return a summary.

    Per-row failures are caught (the agent's invoke contract) so one
    bad capture doesn't abort the tick.
    """
    summary = IngestSummary(listed=len(captures))
    for capture in captures:
        try:
            outcome = agent.invoke(capture)
        except Exception:
            log.exception(
                "captures_materializer.invoke_unexpected_raise record_id=%s",
                capture.record_id,
            )
            summary.failures += 1
            continue
        if outcome.error is None:
            summary.materialized += 1
            if outcome.triage_published:
                summary.triage_published += 1
            summary.deleted += 1
        else:
            summary.failures += 1
    return summary


def _augment_error(outcome: MaterializeOutcome, additional: str) -> MaterializeOutcome:
    """Append a downstream failure (flip / delete) onto an otherwise-OK outcome."""
    new_error = additional if outcome.error is None else f"{outcome.error}; {additional}"
    return MaterializeOutcome(
        record_id=outcome.record_id,
        kind=outcome.kind,
        scope=outcome.scope,
        bq_written=outcome.bq_written,
        triage_published=outcome.triage_published,
        target_table=outcome.target_table,
        error=new_error,
    )
