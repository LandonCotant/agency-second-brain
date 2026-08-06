"""Base agent class — every WS-G agent subclasses this.

PRD §6.1 contract (non-negotiable):
- HIPAA pre-flight check (aborts with HIPAA_GUARD_TRIPPED on hipaa_excluded aspect)
- Audit log emission on every invocation, success or failure
- Prompt loading from `prompts/`, version pinned, never inline
- Confidence < 0.7 -> human review queue, regardless of agent intent
- Memory Bank namespace helpers using the documented convention

Cost tracking, retries, and circuit breakers are NOT here — Agent Runtime SDK
+ Agent Observability handle those (PRD §6.1).
"""

from __future__ import annotations

import time
import traceback
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from ..common.audit_log import AuditLogClient
from ..common.memory_bank import MemoryBank, build_namespace
from ..common.models import AgentInput, AgentOutput, AuditEvent, HipaaGuardStatus
from ..common.prompts import load_prompt

CONFIDENCE_THRESHOLD = 0.7
HIPAA_EXCLUDED_ASPECT = "hipaa_excluded"


class HipaaGuardTripped(RuntimeError):
    """Raised by the base class HIPAA pre-flight. Subclasses must not catch it.

    The audit row is emitted before this is raised, so caller-side handling
    (or lack thereof) cannot drop the audit trail.
    """


class BaseAgent[InputT: AgentInput, OutputT: AgentOutput](ABC):
    def __init__(
        self,
        agent_id: str,
        sa_email: str,
        audit_log: AuditLogClient,
        memory_bank: MemoryBank,
        agent_identity_uuid: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.sa_email = sa_email
        self._audit_log = audit_log
        self._memory_bank = memory_bank
        self._agent_identity_uuid = agent_identity_uuid

    # ------------------------------------------------------------------ public

    def invoke(self, input: InputT) -> OutputT:
        started = time.perf_counter()

        if HIPAA_EXCLUDED_ASPECT in input.aspects:
            self._emit(
                started_perf=started,
                hipaa_guard_status=HipaaGuardStatus.TRIPPED,
                success=False,
                output=None,
                confidence=None,
                human_review_routed=False,
                error=f"{HIPAA_EXCLUDED_ASPECT} aspect present on input",
                input_summary=self._summarize_input(input),
            )
            raise HipaaGuardTripped(
                f"agent {self.agent_id}: input carries {HIPAA_EXCLUDED_ASPECT} aspect"
            )

        try:
            output = self._run(input)
        except Exception as exc:
            self._emit(
                started_perf=started,
                hipaa_guard_status=HipaaGuardStatus.PASSED,
                success=False,
                output=None,
                confidence=None,
                human_review_routed=False,
                error=f"{type(exc).__name__}: {exc}",
                input_summary=self._summarize_input(input),
            )
            raise

        confidence = float(output.confidence)
        human_review_routed = confidence < CONFIDENCE_THRESHOLD
        self._emit(
            started_perf=started,
            hipaa_guard_status=HipaaGuardStatus.PASSED,
            success=True,
            output=self._summarize_output(output),
            confidence=confidence,
            human_review_routed=human_review_routed,
            error=None,
            input_summary=self._summarize_input(input),
        )
        return output

    # --------------------------------------------------------------- subclasses

    @abstractmethod
    def _run(self, input: InputT) -> OutputT:
        """Subclasses implement the actual agent logic here."""

    # ------------------------------------------------------------- prompt + MB

    def _load_prompt(self, name: str, version: str) -> str:
        return load_prompt(name, version)

    def _mb_read(self, entity_id: str, key: str) -> dict[str, Any] | None:
        return self._memory_bank.read(build_namespace(self.agent_id, entity_id), key)

    def _mb_write(self, entity_id: str, key: str, value: dict[str, Any]) -> None:
        self._memory_bank.write(build_namespace(self.agent_id, entity_id), key, value)

    # ---------------------------------------------------------- summarization

    def _summarize_input(self, input: InputT) -> str | None:
        """Default: no input summary written to audit log.

        Override in subclasses to capture input shape *without PII*. The audit
        log is queried by the operator's IAM, but we still avoid storing raw inputs
        because (a) some inputs are large, (b) PRD §4.6 expects a summary.
        """
        return None

    def _summarize_output(self, output: OutputT) -> str | None:
        """Default: no output summary. Override per agent to JSON-serialize."""
        return None

    # ------------------------------------------------------------------ helper

    def _emit(
        self,
        *,
        started_perf: float,
        hipaa_guard_status: HipaaGuardStatus,
        success: bool,
        output: str | None,
        confidence: float | None,
        human_review_routed: bool,
        error: str | None,
        input_summary: str | None,
    ) -> None:
        latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC),
            agent_id=self.agent_id,
            sa_email=self.sa_email,
            latency_ms=latency_ms,
            hipaa_guard_status=hipaa_guard_status,
            success=success,
            human_review_routed=human_review_routed,
            agent_identity_uuid=self._agent_identity_uuid,
            input_summary=input_summary,
            output=output,
            confidence=confidence,
            error=error,
        )
        try:
            self._audit_log.emit(event)
        except Exception:
            # Audit-log write failure is itself an alertable event (PRD §8.2)
            # owned by WS-E. Don't swallow it silently — print the traceback
            # so platform logs surface it, then re-raise so the caller knows.
            traceback.print_exc()
            raise
