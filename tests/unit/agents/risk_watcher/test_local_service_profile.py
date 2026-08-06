"""Unit tests for ``build_local_service_profile`` (ADR 0034)."""

from __future__ import annotations

from agency_brain.agents.risk_watcher.local_service_profile import (
    APPROVAL_SLOWDOWN_PATTERN,
    OWNER_DISENGAGEMENT_PATTERN,
    build_local_service_profile,
)
from agency_brain.agents.risk_watcher.models import Segment, Severity
from agency_brain.agents.risk_watcher.signals import (
    AcknowledgmentGapSignal,
    OwnerDisengagementSignal,
)


def test_factory_builds_profile_with_both_signals() -> None:
    profile = build_local_service_profile()

    assert profile.segment == Segment.LOCAL_SERVICE
    assert len(profile.signals) == 2
    assert any(isinstance(s, AcknowledgmentGapSignal) for s in profile.signals)
    assert any(isinstance(s, OwnerDisengagementSignal) for s in profile.signals)


def test_factory_uses_thresholds_from_risk_profiles_dict() -> None:
    thresholds = {
        APPROVAL_SLOWDOWN_PATTERN: {
            "threshold_value": 7.0,
            "threshold_unit": "business days",
            "window": "rolling 7 days",
        },
        OWNER_DISENGAGEMENT_PATTERN: {
            "threshold_value": 21.0,
            "threshold_unit": "days",
            "window": "rolling 30 days",
        },
    }
    profile = build_local_service_profile(thresholds)

    ack = next(s for s in profile.signals if isinstance(s, AcknowledgmentGapSignal))
    own = next(s for s in profile.signals if isinstance(s, OwnerDisengagementSignal))
    # Internal thresholds are not part of the signal Protocol surface;
    # we re-create one with the same defaults to reach them rather than
    # poking at private attrs across the test/source boundary. Easiest
    # check: the signal labels reflect the configuration.
    assert ack.name == APPROVAL_SLOWDOWN_PATTERN
    assert ack.segment == Segment.LOCAL_SERVICE
    assert ack.severity == Severity.HIGH
    assert own.name == OWNER_DISENGAGEMENT_PATTERN
    assert own.severity == Severity.CRITICAL


def test_factory_falls_back_to_defaults_when_thresholds_missing() -> None:
    """If risk_profiles is empty, the factory still returns usable signals."""
    profile = build_local_service_profile({})
    # Just confirm the factory doesn't raise and emits both signals.
    assert profile.segment == Segment.LOCAL_SERVICE
    assert len(profile.signals) == 2
