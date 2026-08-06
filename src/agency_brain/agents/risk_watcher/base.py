"""``RiskWatcher`` — base agent class for the WS-G2 family (ADR 0033).

Subclass once per profile (PR-B: `EcommerceWatcher`; PR-D/E:
`LocalServiceWatcher`, `AgencyPartnerWatcher`). The base class:

- runs each ``Signal`` against each ``ClientState`` in the input;
- aggregates fired flags into a single ``RiskWatcherOutput``;
- delegates BQ insert + Memory Bank baseline write to subclass-injected
  collaborators (see ``RiskWatcher.__init__`` keyword args).

PR-A (this file) only implements the in-memory loop + the BaseAgent
audit contract. The Cloud Run Job entrypoint, BQ writer wiring, and
Memory Bank baseline pull/push live in PR-B's ``main.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...common.audit_log import AuditLogClient
from ...common.memory_bank import MemoryBank, build_namespace
from ..base import CONFIDENCE_THRESHOLD, BaseAgent
from .models import (
    Flag,
    Profile,
    RiskWatcherInput,
    RiskWatcherOutput,
    utc_now,
)

BASELINE_KEY = "baseline"
"""Memory Bank key under namespace `risk-watcher/{account_id_lower}/baseline`."""


def _ns_segment(account_id: str) -> str:
    """Make an Airtable record id (`recAbCdEf...`) namespace-safe.

    `common/memory_bank.build_namespace` rejects uppercase letters
    (see test_memory_bank.py:28). Airtable record ids are mixed-case
    14-char base62, so lowercasing is a one-way map with no realistic
    collision risk for our scale.
    """
    return account_id.lower()


class RiskWatcher(BaseAgent[RiskWatcherInput, RiskWatcherOutput]):
    """One Risk Watcher instance corresponds to one ``Profile``.

    Concrete subclasses pin ``profile`` and (later) override
    ``_load_client_state`` / ``_persist_baseline`` if they need
    profile-specific I/O. The base class is responsible for the
    evaluate-and-aggregate loop only.
    """

    profile: Profile

    def __init__(
        self,
        *,
        agent_id: str,
        sa_email: str,
        audit_log: AuditLogClient,
        memory_bank: MemoryBank,
        profile: Profile,
        agent_identity_uuid: str | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
            agent_identity_uuid=agent_identity_uuid,
        )
        self.profile = profile

    # --------------------------------------------------------- public

    def _run(self, input: RiskWatcherInput) -> RiskWatcherOutput:
        fired: list[Flag] = []
        for state in input.states:
            for signal in self.profile.signals:
                flag = signal.evaluate(state)
                if flag is not None:
                    fired.append(flag)

        # No-flag-fired tick = high-confidence "nothing happened".
        # BaseAgent's CONFIDENCE_THRESHOLD wouldn't route an empty
        # tick to human review.
        if fired:
            confidence = min(f.confidence for f in fired)
        else:
            confidence = 1.0

        return RiskWatcherOutput(
            flags=tuple(fired),
            confidence=confidence,
            accounts_evaluated=len(input.states),
        )

    # ------------------------------------------------------- helpers

    def read_baseline(self, account_id: str) -> dict[str, Any] | None:
        """Read the Memory Bank baseline for one account.

        Namespace per ADR 0033 §3:
        ``risk-watcher/{account_id_lower}/baseline``.
        """
        ns = build_namespace(self.agent_id, _ns_segment(account_id), BASELINE_KEY)
        return self._memory_bank.read(ns, BASELINE_KEY)

    def write_baseline(self, account_id: str, baseline: dict[str, Any]) -> None:
        ns = build_namespace(self.agent_id, _ns_segment(account_id), BASELINE_KEY)
        self._memory_bank.write(ns, BASELINE_KEY, baseline)

    def _summarize_input(self, input: RiskWatcherInput) -> str | None:
        """Audit-row input summary — count + segment breakdown only.

        Account names are not surfaced because the audit log is
        queryable; per PRD §4.6 we summarize, not echo.
        """
        if not input.states:
            return None
        return (
            f"states={len(input.states)} "
            f"segments={sorted({s.segment.value for s in input.states})}"
        )

    def _summarize_output(self, output: RiskWatcherOutput) -> str | None:
        return (
            f"flags_fired={len(output.flags)} "
            f"accounts_evaluated={output.accounts_evaluated} "
            f"min_confidence={output.confidence:.2f}"
        )

    # --------------------------------------------------- flag building

    @staticmethod
    def materialize_flag_row(flag: Flag, *, model: str, prompt_version: str) -> dict[str, Any]:
        """Convert a Signal-emitted ``Flag`` into a BQ-shaped row.

        Fills the BaseAgent-managed columns
        (``flag_id``, ``flagged_at``, ``agent_run_id``,
        ``human_review_routed``) so the writer can stay single-purpose.
        """
        return {
            "flag_id": str(uuid.uuid4()),
            "flagged_at": utc_now().isoformat(),
            "agent_run_id": str(uuid.uuid4()),
            "account_id": flag.account_id,
            "project_id": flag.project_id,
            "segment": flag.segment.value,
            "pattern_name": flag.pattern_name,
            "severity": flag.severity.value,
            "signal_evidence": flag.signal_evidence,
            "data_sources": list(flag.data_sources),
            "baseline_snapshot": flag.baseline_snapshot,
            "confidence": flag.confidence,
            "human_review_routed": flag.confidence < CONFIDENCE_THRESHOLD,
            "reasoning": flag.reasoning,
            "airtable_task_record_id": None,
            "resolved_at": None,
            "resolution_note": None,
            "model": model,
            "prompt_version": prompt_version,
        }
