"""Shared dataclasses used by the base agent and audit log client.

PRD §6.1 (base class) and §4.6 (audit log schema) are the source of truth.
The dataclasses here mirror the `agent_audit_log.events` schema declared in
`terraform/modules/agent_runtime/main.tf`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


class HipaaGuardStatus(enum.StrEnum):
    PASSED = "PASSED"
    TRIPPED = "TRIPPED"


@runtime_checkable
class AgentInput(Protocol):
    """Loose input contract — every WS-G agent defines a concrete dataclass.

    Base class only requires `aspects` for the HIPAA pre-flight check.
    """

    aspects: list[str]


@runtime_checkable
class AgentOutput(Protocol):
    """Loose output contract — every WS-G agent defines a concrete dataclass.

    Base class only requires `confidence` for the human-review threshold.
    """

    confidence: float


@dataclass(frozen=True)
class AuditEvent:
    """One row in `agent_audit_log.events`. Field order matches the BQ schema.

    `timestamp` must be timezone-aware (UTC). `to_bq_row` serializes it to an
    ISO 8601 string, which BigQuery accepts for streaming inserts.
    """

    event_id: str
    timestamp: datetime
    agent_id: str
    sa_email: str
    latency_ms: int
    hipaa_guard_status: HipaaGuardStatus
    success: bool
    human_review_routed: bool
    agent_identity_uuid: str | None = None
    input_summary: str | None = None
    output: str | None = None
    confidence: float | None = None
    cost_usd: float | None = None
    model_armor_findings: str | None = None
    error: str | None = None

    def to_bq_row(self) -> dict[str, Any]:
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return {
            "event_id": self.event_id,
            "timestamp": ts.isoformat(),
            "agent_id": self.agent_id,
            "agent_identity_uuid": self.agent_identity_uuid,
            "sa_email": self.sa_email,
            "input_summary": self.input_summary,
            "output": self.output,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "hipaa_guard_status": self.hipaa_guard_status.value,
            "model_armor_findings": self.model_armor_findings,
            "success": self.success,
            "error": self.error,
            "human_review_routed": self.human_review_routed,
        }
