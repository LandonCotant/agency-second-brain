"""Personal Re-engagement signal — flags personal contacts the user
hasn't contacted within their relationship-type recency threshold.

ADR 0042 (PKM merge Phase 4). Mirrors the deterministic-threshold
shape of ``OwnerDisengagementSignal`` (ADR 0035) but keys thresholds
off ``Relationship Type`` instead of segment.

Inputs from ``ClientState.extras``:

- ``relationship_type`` (str): one of ``Friend``, ``Mentor``,
  ``Mentee``, ``Collaborator``, ``Family``, ``Other``. Selects the
  threshold from ``DEFAULT_THRESHOLDS`` (constructor-overridable).
- ``warmth`` (str | None): ``Hot`` / ``Warm`` / ``Cool`` / ``Cold``
  or None. Drives severity escalation.
- ``days_since_contact`` (int | None): pre-computed by the loader
  (``DATE_DIFF(CURRENT_DATE(), last_contact, DAY)``). None when
  ``last_contact IS NULL``.
- ``days_since_created`` (int | None): pre-computed by the loader.
  Used as a recency proxy when ``last_contact IS NULL`` so a
  freshly-added contact doesn't false-fire before its first logged
  contact.

Why deterministic, no LLM: this is integer threshold math against
manual relationship-type metadata. An LLM adds zero value and a
nonzero per-tick cost.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..models import ClientState, Flag, Segment, Severity, utc_now

DEFAULT_THRESHOLDS: Mapping[str, int] = {
    "Friend": 60,
    "Mentor": 30,
    "Mentee": 30,
    "Collaborator": 21,
    "Family": 90,
    "Other": 60,
}


class PersonalReEngagementSignal:
    """One contact past their relationship-type threshold = one flag.

    Default severity is ``MEDIUM`` to satisfy the ``Signal`` Protocol;
    actual per-flag severity is recomputed in ``evaluate`` based on
    days-since vs. 2× threshold and Warmth.
    """

    name = "Personal Re-engagement"
    severity = Severity.MEDIUM

    def __init__(
        self,
        *,
        default_thresholds: Mapping[str, int] | None = None,
        now_fn: type[utc_now] | None = None,
    ) -> None:
        self._thresholds = default_thresholds or DEFAULT_THRESHOLDS
        self._now_fn = now_fn or utc_now

    def evaluate(self, client_state: ClientState) -> Flag | None:
        relationship_type = client_state.extras.get("relationship_type")
        if not relationship_type:
            # Loader gates on this being non-null; defensive short-circuit.
            return None

        threshold = self._thresholds.get(
            relationship_type,
            self._thresholds.get("Other", 60),
        )

        warmth = client_state.extras.get("warmth")
        days_since_contact = client_state.extras.get("days_since_contact")
        days_since_created = client_state.extras.get("days_since_created")

        if days_since_contact is not None:
            days = days_since_contact
            if days < threshold:
                return None
            evidence = (
                f"Last contact with this {relationship_type.lower()} was "
                f"{days} days ago (threshold {threshold} days)."
            )
        else:
            # No recorded last_contact — fall back to created-at proxy
            # so we don't nag about contacts the user just added.
            if days_since_created is None or days_since_created < threshold:
                return None
            days = days_since_created
            evidence = (
                f"No recorded last contact since this "
                f"{relationship_type.lower()} was added {days} days ago."
            )

        is_2x = days >= 2 * threshold
        is_warm = warmth in ("Hot", "Warm")
        if is_2x and is_warm:
            severity = Severity.HIGH
        elif is_2x or warmth == "Hot":
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        reasoning = (
            "Personal CRM segment flags 'Personal Re-engagement' when the "
            "user has not contacted a tracked personal connection within "
            "the recency threshold for that Relationship Type. Defaults: "
            "Friend/Other 60d, Mentor/Mentee 30d, Collaborator 21d, "
            "Family 90d. Warmth (Hot/Warm) escalates severity at 2x "
            "threshold; default severity is low so quiet Personal flags "
            "accumulate as Gmail drafts only and do not interrupt the "
            "work-priority Chat batch."
        )

        return Flag(
            pattern_name=self.name,
            severity=severity,
            account_id=client_state.account_id,
            project_id=client_state.project_id,
            segment=Segment.PERSONAL,
            signal_evidence=evidence,
            reasoning=reasoning,
            confidence=0.85,
            data_sources=("airtable_replica.contacts",),
            baseline_snapshot=None,
        )
