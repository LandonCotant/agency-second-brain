"""E-commerce risk profile.

Per ADR 0033 §2 and PRD §6.4, an instance is a tuple of ``Signal``
instances evaluated against each ``ClientState`` per tick. PR-B wires
the two signals that work against current data
(``airtable_replica.tasks`` + ``agent_outputs.triaged_items``):

- ``AcknowledgmentGapSignal`` — Triage-drafted tasks unactioned past
  the per-profile business-day threshold.
- ``SilentAfterDeliverableSignal`` — completed deliverable followed by
  no fresh inbound for the threshold window.

Klaviyo List Decline + ROAS Trend Down (also configured in
``airtable_replica.risk_profiles``) are deferred until the Vantage /
Shopify federation tables ship per WS-B PR-4.
"""

from __future__ import annotations

from typing import Any

from .models import Profile, Segment
from .signals import AcknowledgmentGapSignal, SilentAfterDeliverableSignal


def build_ecommerce_profile(
    thresholds: dict[str, dict[str, Any]] | None = None,
) -> Profile:
    """Build the active E-commerce profile from a thresholds dict.

    ``thresholds`` shape comes from
    ``RiskProfileThresholdsLoader.load(Segment.ECOMMERCE)`` — a dict
    keyed by `pattern_name` carrying ``threshold_value`` (float) +
    ``threshold_unit`` + ``window``. Missing keys fall back to the
    PR-B defaults so the loader can be off-by-one tolerant.
    """
    thresholds = thresholds or {}

    # Pattern names match `airtable_replica.risk_profiles.pattern_name`
    # for the E-commerce segment. ADR 0034 §2 lifted these from class
    # attributes to instance attributes on AcknowledgmentGapSignal so
    # the same class can be reused under "Approval Slowdown" for
    # Local Service.
    ack_threshold = int(thresholds.get("Acknowledgment Gap", {}).get("threshold_value", 5))
    silent_threshold = int(
        thresholds.get(SilentAfterDeliverableSignal.name, {}).get("threshold_value", 5)
    )

    return Profile(
        segment=Segment.ECOMMERCE,
        signals=(
            AcknowledgmentGapSignal(business_days_threshold=ack_threshold),
            SilentAfterDeliverableSignal(business_days_threshold=silent_threshold),
        ),
    )


# Default profile (PR-B fallback thresholds). The Cloud Run Job
# entrypoint calls ``build_ecommerce_profile(thresholds)`` with the
# Airtable-loaded values at startup.
ECOMMERCE_PROFILE: Profile = build_ecommerce_profile()
