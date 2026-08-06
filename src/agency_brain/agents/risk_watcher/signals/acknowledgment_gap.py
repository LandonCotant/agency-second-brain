"""Acknowledgment Gap signal — fires when an inbound signal sits in
`Drafted by Agent` state past the configured business-day threshold
(default 5, per `airtable_replica.risk_profiles`).

Inputs come from ``ClientState.extras["pending_drafted_tasks"]``: a
list of dicts ``{record_id, task_name, created}`` for tasks the
Triage Agent drafted but the operator hasn't reviewed yet. The loader
filters to tasks under the account's projects.

Why deterministic, no LLM: the rule is pure calendar math. A future
PR can swap to LLM-based ranking if the signal volume gets noisy, but
the threshold + business-day rule is the source-of-truth signal
configuration in `risk_profiles`.

Cross-segment reuse (ADR 0034 §2): ``pattern_name``, ``segment``, and
``severity`` are constructor params. The Local Service "Approval
Slowdown" signal is the same rule with different labels, so both
profiles instantiate this class with their own configuration rather
than subclassing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..business_days import business_days_between
from ..models import ClientState, Flag, Segment, Severity, utc_now


class AcknowledgmentGapSignal:
    """One drafted Triage task older than the threshold = one fired flag.

    The signal aggregates over all pending drafted tasks for the
    account: ``signal_evidence`` lists the count + the oldest task's
    age, which is enough for a Chat card / Gmail draft to be
    actionable without a per-task firing.
    """

    def __init__(
        self,
        *,
        business_days_threshold: int = 5,
        pattern_name: str = "Acknowledgment Gap",
        segment: Segment = Segment.ECOMMERCE,
        severity: Severity = Severity.HIGH,
        now_fn: type[utc_now] | None = None,
    ) -> None:
        self._threshold = business_days_threshold
        self.name = pattern_name
        self.segment = segment
        self.severity = severity
        self._now_fn = now_fn or utc_now

    def evaluate(self, client_state: ClientState) -> Flag | None:
        pending = client_state.extras.get("pending_drafted_tasks") or []
        if not pending:
            return None

        now = self._now_fn()  # type: ignore[operator]
        # ``pending`` is sorted oldest-first by the loader.
        oldest = pending[0]
        oldest_created = _coerce_dt(oldest["created"])
        oldest_age = business_days_between(oldest_created, now)

        if oldest_age < self._threshold:
            return None

        # Count how many cross the threshold so the Chat card can say
        # "3 drafts overdue (oldest 7 business days)" rather than just
        # the oldest one.
        overdue = [
            p
            for p in pending
            if business_days_between(_coerce_dt(p["created"]), now) >= self._threshold
        ]

        evidence = (
            f"{len(overdue)} drafted task(s) overdue beyond "
            f"{self._threshold} business days; oldest is {oldest_age} "
            f"business days ({oldest['task_name']!r}, "
            f"record {oldest['record_id']})."
        )
        reasoning = (
            f"{self.segment.value} risk profile flags '{self.name}' when "
            "a Triage-drafted Task remains unactioned past the configured "
            "business-day threshold. A drafted task means the inbound "
            "signal landed in BQ + Airtable; pending review past the "
            "threshold means the human-review loop is at risk of breach."
        )
        return Flag(
            pattern_name=self.name,
            severity=self.severity,
            account_id=client_state.account_id,
            project_id=client_state.project_id,
            segment=self.segment,
            signal_evidence=evidence,
            reasoning=reasoning,
            confidence=0.95,
            data_sources=("airtable_replica.tasks",),
            baseline_snapshot=None,
        )


def _coerce_dt(value: Any) -> datetime:
    """Loader passes tz-aware datetimes; tests sometimes pass strings."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # `fromisoformat` accepts both `+00:00` and (since 3.11) `Z`.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unexpected created-at type: {type(value).__name__}")
