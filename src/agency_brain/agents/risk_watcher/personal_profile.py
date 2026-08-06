"""Personal-segment risk profile (ADR 0042).

The Personal segment carries a single signal in v1:
``PersonalReEngagementSignal``. Per-relationship-type thresholds
live inside the signal; the ``thresholds`` keyword on
``build_personal_profile`` is reserved for future
``airtable_replica.risk_profiles`` overrides but currently unused
(symmetry with ``build_ecommerce_profile`` so the multi-segment
loop can pass thresholds to every profile uniformly).
"""

from __future__ import annotations

from typing import Any

from .models import Profile, Segment
from .signals import PersonalReEngagementSignal


def build_personal_profile(
    thresholds: dict[str, dict[str, Any]] | None = None,
) -> Profile:
    """Build the Personal segment profile.

    ``thresholds`` is accepted for API symmetry with the other
    profile builders. v1 ignores it — the per-relationship-type
    defaults live in ``PersonalReEngagementSignal.DEFAULT_THRESHOLDS``.
    Reserved for a future ``risk_profiles.pattern_name='Personal
    Re-engagement'`` row that would carry per-relationship overrides.
    """
    _ = thresholds  # reserved
    return Profile(
        segment=Segment.PERSONAL,
        signals=(PersonalReEngagementSignal(),),
    )


PERSONAL_PROFILE: Profile = build_personal_profile()
