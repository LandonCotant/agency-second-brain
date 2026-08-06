"""Unit tests for ``build_agency_partner_profile`` (ADR 0034 §5)."""

from __future__ import annotations

from agency_brain.agents.risk_watcher.agency_partner_profile import (
    AGENCY_PARTNER_PROFILE,
    build_agency_partner_profile,
)
from agency_brain.agents.risk_watcher.models import Segment


def test_factory_returns_empty_signal_tuple() -> None:
    """ADR 0034 §5: AP profile is a structural skeleton until Vantage lands."""
    profile = build_agency_partner_profile()
    assert profile.segment == Segment.AGENCY_PARTNER
    assert profile.signals == ()


def test_default_profile_constant_is_empty() -> None:
    assert AGENCY_PARTNER_PROFILE.segment == Segment.AGENCY_PARTNER
    assert AGENCY_PARTNER_PROFILE.signals == ()


def test_factory_accepts_thresholds_dict_without_error() -> None:
    """Call shape matches the other two profile factories so `main.py`
    doesn't have to special-case AP."""
    profile = build_agency_partner_profile(
        {
            "Raw Data Inquiry": {"threshold_value": 1.0},
            "Refinement Silence": {"threshold_value": 14.0},
        }
    )
    # Thresholds ignored; profile stays empty until Vantage signals
    # are wired in a future PR.
    assert profile.signals == ()
