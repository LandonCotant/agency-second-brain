"""Unit tests for ``PersonalReEngagementSignal`` (ADR 0042)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Segment,
    Severity,
)
from agency_brain.agents.risk_watcher.signals import (
    PersonalReEngagementSignal,
)


def _now() -> datetime:
    return datetime(2026, 5, 11, 12, tzinfo=UTC)


def _state(extras: dict[str, Any]) -> ClientState:
    return ClientState(
        account_id="recContact1",
        account_name="Alex Mentor",
        segment=Segment.PERSONAL,
        project_id=None,
        extras=extras,
    )


def test_recent_contact_under_threshold_returns_none() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",  # threshold 60
        "warmth": "Warm",
        "days_since_contact": 14,
        "days_since_created": 365,
    }
    assert signal.evaluate(_state(extras)) is None


def test_at_threshold_fires_low_severity() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",  # threshold 60
        "warmth": "Cool",
        "days_since_contact": 60,
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.pattern_name == "Personal Re-engagement"
    assert flag.severity == Severity.LOW
    assert flag.segment == Segment.PERSONAL
    assert flag.account_id == "recContact1"
    assert "60 days ago" in flag.signal_evidence
    assert "threshold 60 days" in flag.signal_evidence
    assert flag.confidence == 0.85
    assert "airtable_replica.contacts" in flag.data_sources


def test_2x_threshold_with_hot_warmth_fires_high() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Mentor",  # threshold 30
        "warmth": "Hot",
        "days_since_contact": 70,  # > 2*30 = 60
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.HIGH


def test_2x_threshold_with_warm_warmth_fires_high() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Collaborator",  # threshold 21
        "warmth": "Warm",
        "days_since_contact": 50,  # > 2*21 = 42
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.HIGH


def test_2x_threshold_with_cool_warmth_fires_medium() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Family",  # threshold 90
        "warmth": "Cool",
        "days_since_contact": 200,  # > 2*90 = 180
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.MEDIUM


def test_hot_warmth_below_2x_fires_medium() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",  # threshold 60
        "warmth": "Hot",
        "days_since_contact": 75,  # > 60 but < 120
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.MEDIUM


def test_no_warmth_above_threshold_fires_low() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Other",  # threshold 60
        "warmth": None,
        "days_since_contact": 75,
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.LOW


def test_null_relationship_type_returns_none() -> None:
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": None,
        "warmth": "Hot",
        "days_since_contact": 100,
        "days_since_created": 365,
    }
    assert signal.evaluate(_state(extras)) is None


def test_unknown_relationship_type_uses_other_default() -> None:
    """Defensive: a typo or new singleSelect option falls back to Other (60d)."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Confidant",  # not in DEFAULT_THRESHOLDS
        "warmth": "Cool",
        "days_since_contact": 75,  # > 60 (Other default)
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert "75 days ago" in flag.signal_evidence
    assert "threshold 60 days" in flag.signal_evidence


def test_null_last_contact_with_old_creation_fires_low() -> None:
    """No recorded last_contact, contact added > threshold ago: fire on
    creation proxy with low severity."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Mentor",  # threshold 30
        "warmth": None,
        "days_since_contact": None,
        "days_since_created": 45,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert flag.severity == Severity.LOW
    assert "No recorded last contact" in flag.signal_evidence
    assert "added 45 days ago" in flag.signal_evidence


def test_null_last_contact_with_fresh_creation_returns_none() -> None:
    """Brand-new contact, never logged a last_contact: don't false-fire."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",  # threshold 60
        "warmth": "Hot",
        "days_since_contact": None,
        "days_since_created": 14,
    }
    assert signal.evaluate(_state(extras)) is None


def test_null_last_contact_and_null_creation_returns_none() -> None:
    """Loader couldn't compute either proxy: defensive no-fire."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",
        "warmth": "Hot",
        "days_since_contact": None,
        "days_since_created": None,
    }
    assert signal.evaluate(_state(extras)) is None


def test_negative_days_since_contact_does_not_fire() -> None:
    """Future last_contact (data entry error or scheduled future): quiet."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Friend",
        "warmth": "Hot",
        "days_since_contact": -3,
        "days_since_created": 365,
    }
    assert signal.evaluate(_state(extras)) is None


def test_constructor_threshold_override_applies() -> None:
    """A caller can pass tighter thresholds — useful for power users."""
    signal = PersonalReEngagementSignal(
        default_thresholds={"Friend": 14, "Other": 14},
        now_fn=_now,
    )
    extras = {
        "relationship_type": "Friend",
        "warmth": "Cool",
        "days_since_contact": 20,  # > 14 (override) but < 60 (default)
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert "threshold 14 days" in flag.signal_evidence


def test_evidence_lowercases_relationship_type_in_prose() -> None:
    """User-facing evidence reads naturally."""
    signal = PersonalReEngagementSignal(now_fn=_now)
    extras = {
        "relationship_type": "Mentor",
        "warmth": "Cool",
        "days_since_contact": 45,
        "days_since_created": 365,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert "this mentor was" in flag.signal_evidence
