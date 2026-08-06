"""Local Service risk profile (ADR 0034).

Per ADR 0034 §5, two signals ship in PR-D against today's data
sources (`airtable_replica.*` + Calendar via DWD):

- ``AcknowledgmentGapSignal`` reused with ``pattern_name="Approval
  Slowdown"`` — drafted Triage tasks unactioned past the threshold.
- ``OwnerDisengagementSignal`` — owner has not met with anyone from
  the client's contact-email domains in the threshold window.

GBP Decline + GBP-flavored Acknowledgment Gap (also configured in
``airtable_replica.risk_profiles`` for Local Service) are deferred
until the Vantage / GBP federation tables ship per WS-B PR-4.
"""

from __future__ import annotations

from typing import Any

from .models import Profile, Segment, Severity
from .signals import AcknowledgmentGapSignal, OwnerDisengagementSignal

APPROVAL_SLOWDOWN_PATTERN = "Approval Slowdown"
OWNER_DISENGAGEMENT_PATTERN = "Owner Disengagement"


def build_local_service_profile(
    thresholds: dict[str, dict[str, Any]] | None = None,
) -> Profile:
    """Build the active Local Service profile from a thresholds dict.

    ``thresholds`` shape comes from
    ``RiskProfileThresholdsLoader.load(Segment.LOCAL_SERVICE)``.
    Missing keys fall back to PR-D defaults so the loader can be
    off-by-one tolerant.
    """
    thresholds = thresholds or {}

    approval_threshold = int(
        thresholds.get(APPROVAL_SLOWDOWN_PATTERN, {}).get("threshold_value", 5)
    )
    disengagement_threshold = int(
        thresholds.get(OWNER_DISENGAGEMENT_PATTERN, {}).get("threshold_value", 14)
    )

    return Profile(
        segment=Segment.LOCAL_SERVICE,
        signals=(
            AcknowledgmentGapSignal(
                business_days_threshold=approval_threshold,
                pattern_name=APPROVAL_SLOWDOWN_PATTERN,
                segment=Segment.LOCAL_SERVICE,
                severity=Severity.HIGH,
            ),
            OwnerDisengagementSignal(
                days_threshold=disengagement_threshold,
                segment=Segment.LOCAL_SERVICE,
            ),
        ),
    )


# Default profile (PR-D fallback thresholds). Production builds the
# profile via ``build_local_service_profile(thresholds)`` at startup
# inside the per-segment loop in `main.py`.
LOCAL_SERVICE_PROFILE: Profile = build_local_service_profile()
