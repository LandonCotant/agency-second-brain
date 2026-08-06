"""Unit tests for ``build_personal_profile`` (ADR 0042)."""

from __future__ import annotations

from agency_brain.agents.risk_watcher.models import Segment
from agency_brain.agents.risk_watcher.personal_profile import (
    PERSONAL_PROFILE,
    build_personal_profile,
)
from agency_brain.agents.risk_watcher.signals import (
    PersonalReEngagementSignal,
)


def test_default_profile_carries_one_signal() -> None:
    profile = PERSONAL_PROFILE
    assert profile.segment == Segment.PERSONAL
    assert len(profile.signals) == 1
    assert isinstance(profile.signals[0], PersonalReEngagementSignal)


def test_thresholds_kwarg_is_accepted_for_api_symmetry() -> None:
    """v1 ignores the kwarg; reserved for risk_profiles override."""
    profile = build_personal_profile(
        thresholds={"Personal Re-engagement": {"threshold_value": 7.0}}
    )
    assert profile.segment == Segment.PERSONAL
    signal = profile.signals[0]
    assert isinstance(signal, PersonalReEngagementSignal)
    # v1: thresholds kwarg is reserved/ignored — defaults still apply.
    assert signal._thresholds["Friend"] == 60
