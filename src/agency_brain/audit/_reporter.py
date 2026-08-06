"""Shared event reporter for the four runtime audit scripts (WS-F PR #2).

Each script runs as a Cloud Run Job on a Cloud Scheduler cadence and emits
exactly one ``agent_audit_log.events`` row per execution, regardless of
outcome (success / drift / error). Mirrors the BaseAgent contract from
``agency_brain.agents.base`` (PRD §4.6 / ADR 0006: "emit on every path").

Two surfaces, one call:

1. **BigQuery audit row.** Uses :class:`AuditLogClient` directly so the
   schema stays in lock-step with what every other agent writes. ``agent_id``
   is set to ``audit-<short>`` and the ``output`` column holds a JSON-encoded
   summary of the check (counts per query, drift rows, etc.).

2. **Structured stdout.** A JSON line with the ``event`` field set to the
   same event_id. The HIPAA isolation alert in
   ``terraform/modules/observability/alerts_hipaa.tf`` matches
   ``jsonPayload.event:"HIPAA_GUARD_TRIPPED"`` — emitting that key here is
   what wires runtime drift into the existing P0 Chat alert without any new
   notification plumbing in PR #2.

The reporter never raises on emit failure — the audit row is best-effort
durable, but a Cloud Run Job that crashes mid-emit must still leave evidence
in stdout (which Cloud Logging captures unconditionally).

Event ids:
- ``HIPAA_GUARD_TRIPPED`` — HIPAA isolation breach detected. Triggers the
  existing log-based metric + P0 alert.
- ``SECURITY_DRIFT`` — IAM / bucket / drafts boundary drift detected.
- ``SECURITY_AUDIT_OK`` — clean run, no drift.
- ``SECURITY_AUDIT_ERROR`` — script failed before completing the check.
- ``COST_AUDIT_OK`` — daily spend run completed, all monitored projects
  under their thresholds (ADR 0030).
- ``COST_THRESHOLD_EXCEEDED`` — at least one monitored project exceeded
  its configured daily threshold (ADR 0030). Triggers a Cloud Monitoring
  alert routed to the Brain alerts Chat space + owner email.
"""

from __future__ import annotations

import enum
import json
import logging
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..common.audit_log import AuditLogClient
from ..common.models import AuditEvent, HipaaGuardStatus

log = logging.getLogger("agency_brain.audit")


class AuditEventId(enum.StrEnum):
    HIPAA_GUARD_TRIPPED = "HIPAA_GUARD_TRIPPED"
    SECURITY_DRIFT = "SECURITY_DRIFT"
    SECURITY_AUDIT_OK = "SECURITY_AUDIT_OK"
    SECURITY_AUDIT_ERROR = "SECURITY_AUDIT_ERROR"
    COST_AUDIT_OK = "COST_AUDIT_OK"
    COST_THRESHOLD_EXCEEDED = "COST_THRESHOLD_EXCEEDED"


@dataclass
class AuditScriptContext:
    """Per-script invariants. Built once at script entry."""

    short_name: str  # e.g. "hipaa-isolation"
    sa_email: str
    project_id: str
    audit_log: AuditLogClient
    started_perf: float

    @property
    def agent_id(self) -> str:
        return f"audit-{self.short_name}"


class DriftReporter:
    """Emit one audit row + one stdout line for the run.

    Construct fresh per script. Call exactly one of :meth:`ok`,
    :meth:`drift`, :meth:`hipaa_breach`, or :meth:`error` — emitting twice is
    a programming error (the runtime audit contract is one row per run).
    """

    def __init__(self, ctx: AuditScriptContext) -> None:
        self._ctx = ctx
        self._emitted = False

    # ------------------------------------------------------------------ public

    def ok(self, summary: dict[str, Any]) -> None:
        self._emit(
            event_id=AuditEventId.SECURITY_AUDIT_OK,
            success=True,
            hipaa_status=HipaaGuardStatus.PASSED,
            summary=summary,
            error=None,
        )

    def drift(self, summary: dict[str, Any]) -> None:
        self._emit(
            event_id=AuditEventId.SECURITY_DRIFT,
            success=True,  # script ran successfully; the *finding* is drift
            hipaa_status=HipaaGuardStatus.PASSED,
            summary=summary,
            error=None,
        )

    def hipaa_breach(self, summary: dict[str, Any]) -> None:
        self._emit(
            event_id=AuditEventId.HIPAA_GUARD_TRIPPED,
            success=True,
            hipaa_status=HipaaGuardStatus.TRIPPED,
            summary=summary,
            error=None,
        )

    def cost_ok(self, summary: dict[str, Any]) -> None:
        self._emit(
            event_id=AuditEventId.COST_AUDIT_OK,
            success=True,
            hipaa_status=HipaaGuardStatus.PASSED,
            summary=summary,
            error=None,
        )

    def cost_breach(self, summary: dict[str, Any]) -> None:
        self._emit(
            event_id=AuditEventId.COST_THRESHOLD_EXCEEDED,
            success=True,
            hipaa_status=HipaaGuardStatus.PASSED,
            summary=summary,
            error=None,
        )

    def error(self, exc: BaseException) -> None:
        self._emit(
            event_id=AuditEventId.SECURITY_AUDIT_ERROR,
            success=False,
            hipaa_status=HipaaGuardStatus.PASSED,
            summary={"error_type": type(exc).__name__, "error_message": str(exc)},
            error=f"{type(exc).__name__}: {exc}",
        )

    # ----------------------------------------------------------------- internal

    def _emit(
        self,
        *,
        event_id: AuditEventId,
        success: bool,
        hipaa_status: HipaaGuardStatus,
        summary: dict[str, Any],
        error: str | None,
    ) -> None:
        if self._emitted:
            raise RuntimeError(
                f"DriftReporter already emitted for {self._ctx.agent_id} — "
                "scripts must emit exactly one event per run"
            )
        self._emitted = True

        latency_ms = max(0, int((time.perf_counter() - self._ctx.started_perf) * 1000))

        self._emit_stdout(event_id=event_id, summary=summary, error=error)

        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            agent_id=self._ctx.agent_id,
            sa_email=self._ctx.sa_email,
            latency_ms=latency_ms,
            hipaa_guard_status=hipaa_status,
            success=success,
            human_review_routed=False,
            output=json.dumps({"event_id": event_id.value, **summary}, default=str),
            error=error,
        )
        try:
            self._ctx.audit_log.emit(event)
        except Exception:
            # The audit log IS this job's product. If the write fails, exit
            # non-zero so asb-cloud-run-job-error fires — a swallowed failure
            # here makes a broken audit trail look healthy (stdout already
            # printed "OK" via _emit_stdout above).
            traceback.print_exc()
            raise

    def _emit_stdout(
        self,
        *,
        event_id: AuditEventId,
        summary: dict[str, Any],
        error: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "severity": "ERROR"
            if event_id is AuditEventId.HIPAA_GUARD_TRIPPED
            else "WARNING"
            if event_id
            in (
                AuditEventId.SECURITY_DRIFT,
                AuditEventId.COST_THRESHOLD_EXCEEDED,
            )
            else "INFO",
            "event": event_id.value,
            "agent_id": self._ctx.agent_id,
            "summary": summary,
        }
        if error is not None:
            payload["error"] = error
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
