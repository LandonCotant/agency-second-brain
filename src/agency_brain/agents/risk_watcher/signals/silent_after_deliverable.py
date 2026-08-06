"""Silent After Deliverable signal — fires when an account has had a
completed deliverable but no fresh inbound triage activity past the
configured business-day threshold (default 5, per
``airtable_replica.risk_profiles``).

Inputs come from ``ClientState.extras``:
- ``last_completed_deliverable_at``: tz-aware datetime of the most
  recent completed Task across the account's projects;
- ``last_inbound_at``: tz-aware datetime of the most recent
  ``agent_outputs.triaged_items`` row keyed to the account, or None.

Rule: deliverable was completed N business days ago AND last_inbound
is older than the deliverable (or absent). N comes from the profile
threshold.

The "or absent" branch matters because a brand-new account with a
delivered project but no inbound history is exactly the silent state
the signal is supposed to catch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..business_days import business_days_between
from ..models import ClientState, Flag, Segment, Severity, utc_now


class SilentAfterDeliverableSignal:
    """Deliverable shipped, no follow-up inbound = at-risk client."""

    name = "Silent After Deliverable"
    severity = Severity.HIGH

    def __init__(
        self,
        *,
        business_days_threshold: int = 5,
        now_fn: type[utc_now] | None = None,
    ) -> None:
        self._threshold = business_days_threshold
        self._now_fn = now_fn or utc_now

    def evaluate(self, client_state: ClientState) -> Flag | None:
        last_delivery = client_state.extras.get("last_completed_deliverable_at")
        if last_delivery is None:
            # No deliverable yet = no signal possible. Brand-new
            # accounts pre-delivery aren't "silent" in the sense that
            # matters.
            return None

        last_delivery_dt = _coerce_dt(last_delivery)
        now = self._now_fn()  # type: ignore[operator]
        days_since_delivery = business_days_between(last_delivery_dt, now)

        if days_since_delivery < self._threshold:
            return None

        last_inbound = client_state.extras.get("last_inbound_at")
        if last_inbound is not None:
            last_inbound_dt = _coerce_dt(last_inbound)
            if last_inbound_dt > last_delivery_dt:
                # Client has reached out since the deliverable — not
                # silent. Threshold gate failed at the inbound check
                # rather than the deliverable check.
                return None

        # Either no inbound at all, or the most recent inbound predates
        # the deliverable. Either way, the post-delivery silence has
        # crossed the threshold.
        evidence = (
            f"Last completed deliverable was {days_since_delivery} "
            f"business days ago ({last_delivery_dt.date().isoformat()}); "
            + (
                "no inbound triage activity since."
                if last_inbound is None
                else f"most recent inbound was {_coerce_dt(last_inbound).date().isoformat()}, "
                f"before the deliverable."
            )
        )
        reasoning = (
            "E-commerce risk profile flags Silent After Deliverable "
            "when a completed task (the deliverable) is followed by "
            "more than the threshold of business days with no inbound "
            "triage activity from the account. Post-delivery silence is "
            "a leading indicator of dissatisfaction or churn."
        )
        return Flag(
            pattern_name=self.name,
            severity=self.severity,
            account_id=client_state.account_id,
            project_id=client_state.project_id,
            segment=Segment.ECOMMERCE,
            signal_evidence=evidence,
            reasoning=reasoning,
            confidence=0.9,
            data_sources=(
                "airtable_replica.tasks",
                "airtable_replica.projects",
            ),
            baseline_snapshot=None,
        )


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unexpected datetime-shaped value: {type(value).__name__}")
