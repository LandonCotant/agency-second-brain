from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agency_brain.common.audit_log import AuditLogClient, AuditLogWriteError
from agency_brain.common.models import AuditEvent, HipaaGuardStatus


class _StubBQ:
    def __init__(self, errors: list | None = None) -> None:
        self.errors = errors or []
        self.calls: list[tuple[str, list[dict]]] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.calls.append((table_ref, rows))
        return self.errors


def _event() -> AuditEvent:
    return AuditEvent(
        event_id="evt-1",
        timestamp=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
        agent_id="triage",
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        latency_ms=42,
        hipaa_guard_status=HipaaGuardStatus.PASSED,
        success=True,
        human_review_routed=False,
        confidence=0.91,
    )


def test_emit_writes_to_correct_table_ref() -> None:
    bq = _StubBQ()
    client = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)

    client.emit(_event())

    assert bq.calls == [
        (
            "agency-brain-demo.agent_audit_log.events",
            [
                {
                    "event_id": "evt-1",
                    "timestamp": "2026-04-25T12:00:00+00:00",
                    "agent_id": "triage",
                    "agent_identity_uuid": None,
                    "sa_email": ("asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com"),
                    "input_summary": None,
                    "output": None,
                    "confidence": 0.91,
                    "latency_ms": 42,
                    "cost_usd": None,
                    "hipaa_guard_status": "PASSED",
                    "model_armor_findings": None,
                    "success": True,
                    "error": None,
                    "human_review_routed": False,
                }
            ],
        )
    ]


def test_emit_raises_on_bq_errors() -> None:
    bq = _StubBQ(errors=[{"index": 0, "errors": [{"reason": "invalid"}]}])
    client = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)

    with pytest.raises(AuditLogWriteError):
        client.emit(_event())


def test_naive_timestamp_is_treated_as_utc() -> None:
    bq = _StubBQ()
    client = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    naive = AuditEvent(
        event_id="evt-2",
        timestamp=datetime(2026, 4, 25, 12, 0, 0),
        agent_id="triage",
        sa_email="x@y",
        latency_ms=1,
        hipaa_guard_status=HipaaGuardStatus.PASSED,
        success=True,
        human_review_routed=False,
    )

    client.emit(naive)

    assert bq.calls[0][1][0]["timestamp"] == "2026-04-25T12:00:00+00:00"
