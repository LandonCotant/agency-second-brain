"""Unit tests for v2 ``OwnerDisengagementSignal`` (ADR 0035)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Segment,
    Severity,
)
from agency_brain.agents.risk_watcher.signals import OwnerDisengagementSignal


def _now() -> datetime:
    return datetime(2026, 5, 11, 12, tzinfo=UTC)


def _state(extras: dict[str, Any]) -> ClientState:
    return ClientState(
        account_id="recAcctLS1",
        account_name="Client A",
        segment=Segment.LOCAL_SERVICE,
        project_id="recProjLS1",
        extras=extras,
    )


def test_recent_engagement_under_threshold_returns_none() -> None:
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": datetime(2026, 5, 4, 12, tzinfo=UTC),  # 7 days
        "engagement_lookback_days": 30,
        "winning_source": "calendar",
    }
    assert signal.evaluate(_state(extras)) is None


def test_old_engagement_at_or_above_threshold_fires() -> None:
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": datetime(2026, 4, 20, 12, tzinfo=UTC),  # 21 days
        "engagement_lookback_days": 30,
        "winning_source": "triage inbound",
    }
    flag = signal.evaluate(_state(extras))

    assert flag is not None
    assert flag.pattern_name == "Owner Disengagement"
    assert flag.severity == Severity.CRITICAL
    assert flag.segment == Segment.LOCAL_SERVICE
    assert flag.account_id == "recAcctLS1"
    assert "21 days ago" in flag.signal_evidence
    assert "via triage inbound" in flag.signal_evidence
    assert flag.confidence == 0.85
    # Multi-source data_sources reflect ADR 0035 §1.
    assert "google_calendar" in flag.data_sources
    assert "airtable_replica.tasks" in flag.data_sources
    assert "airtable_replica.contacts" in flag.data_sources
    assert "airtable_replica.contracts" in flag.data_sources


def test_evidence_omits_via_clause_when_winning_source_unknown() -> None:
    """Older flags / loader edge cases may not record winning_source."""
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": datetime(2026, 4, 20, 12, tzinfo=UTC),
        "engagement_lookback_days": 30,
        "winning_source": None,
    }
    flag = signal.evaluate(_state(extras))
    assert flag is not None
    assert "21 days ago" in flag.signal_evidence
    assert "via" not in flag.signal_evidence


def test_no_engagement_with_full_lookback_fires() -> None:
    """All sources quiet AND lookback >= threshold => fire."""
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": None,
        "engagement_lookback_days": 30,
        "winning_source": None,
    }
    flag = signal.evaluate(_state(extras))

    assert flag is not None
    assert "No engagement" in flag.signal_evidence
    assert "30 days" in flag.signal_evidence


def test_no_engagement_with_short_lookback_does_not_fire() -> None:
    """Avoid false-fire when the loader's lookback < threshold."""
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": None,
        "engagement_lookback_days": 7,
        "winning_source": None,
    }
    assert signal.evaluate(_state(extras)) is None


def test_loader_unable_to_run_does_not_fire() -> None:
    """Both engagement_at and lookback None (e.g. all queries failed)."""
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": None,
        "engagement_lookback_days": None,
        "winning_source": None,
    }
    assert signal.evaluate(_state(extras)) is None


def test_future_engagement_clamps_to_zero_days() -> None:
    """A meeting scheduled but not yet happened doesn't fire as 'old'."""
    signal = OwnerDisengagementSignal(days_threshold=14, now_fn=_now)
    extras = {
        "last_engagement_at": datetime(2026, 5, 20, 12, tzinfo=UTC),  # future
        "engagement_lookback_days": 30,
        "winning_source": "calendar",
    }
    # Future event => 0 days since => under threshold => quiet.
    assert signal.evaluate(_state(extras)) is None
