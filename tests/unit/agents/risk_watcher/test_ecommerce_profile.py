"""Unit tests for ``build_ecommerce_profile``."""

from __future__ import annotations

from agency_brain.agents.risk_watcher.ecommerce_profile import (
    ECOMMERCE_PROFILE,
    build_ecommerce_profile,
)
from agency_brain.agents.risk_watcher.models import Segment
from agency_brain.agents.risk_watcher.signals import (
    AcknowledgmentGapSignal,
    SilentAfterDeliverableSignal,
)


def test_default_profile_carries_two_signals() -> None:
    profile = ECOMMERCE_PROFILE
    assert profile.segment == Segment.ECOMMERCE
    assert len(profile.signals) == 2
    assert any(isinstance(s, AcknowledgmentGapSignal) for s in profile.signals)
    assert any(isinstance(s, SilentAfterDeliverableSignal) for s in profile.signals)


def test_default_thresholds_are_5_business_days() -> None:
    profile = build_ecommerce_profile(thresholds=None)
    ack = next(s for s in profile.signals if isinstance(s, AcknowledgmentGapSignal))
    silent = next(s for s in profile.signals if isinstance(s, SilentAfterDeliverableSignal))
    assert ack._threshold == 5
    assert silent._threshold == 5


def test_thresholds_dict_overrides_defaults() -> None:
    """Threshold loader's float gets cast to int."""
    profile = build_ecommerce_profile(
        thresholds={
            "Acknowledgment Gap": {"threshold_value": 7.0},
            "Silent After Deliverable": {"threshold_value": 3.0},
        }
    )
    ack = next(s for s in profile.signals if isinstance(s, AcknowledgmentGapSignal))
    silent = next(s for s in profile.signals if isinstance(s, SilentAfterDeliverableSignal))
    assert ack._threshold == 7
    assert silent._threshold == 3


def test_partial_thresholds_keeps_defaults_for_missing_keys() -> None:
    profile = build_ecommerce_profile(thresholds={"Acknowledgment Gap": {"threshold_value": 10.0}})
    ack = next(s for s in profile.signals if isinstance(s, AcknowledgmentGapSignal))
    silent = next(s for s in profile.signals if isinstance(s, SilentAfterDeliverableSignal))
    assert ack._threshold == 10
    assert silent._threshold == 5  # default
