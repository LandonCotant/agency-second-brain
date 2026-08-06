"""Unit tests for ``SilentAfterDeliverableSignal``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Segment,
    Severity,
)
from agency_brain.agents.risk_watcher.signals import (
    SilentAfterDeliverableSignal,
)


def _state(extras: dict[str, Any]) -> ClientState:
    return ClientState(
        account_id="recAcct1",
        account_name="Acme Co",
        segment=Segment.ECOMMERCE,
        project_id="recProj1",
        extras=extras,
    )


def _now() -> datetime:
    # 2026-05-11 (Mon)
    return datetime(2026, 5, 11, 12, tzinfo=UTC)


def test_no_deliverable_returns_none() -> None:
    """Brand-new account with no deliverable yet => no signal possible."""
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(extras={})) is None
    assert signal.evaluate(_state(extras={"last_completed_deliverable_at": None})) is None


def test_deliverable_within_threshold_returns_none() -> None:
    """Deliverable yesterday => 1 bday, well under 5."""
    extras = {
        "last_completed_deliverable_at": datetime(2026, 5, 8, tzinfo=UTC),  # Fri
        "last_inbound_at": None,
    }
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(extras)) is None


def test_old_deliverable_no_inbound_fires() -> None:
    extras = {
        "last_completed_deliverable_at": datetime(2026, 4, 27, tzinfo=UTC),  # Mon
        "last_inbound_at": None,
    }
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    flag = signal.evaluate(_state(extras))

    assert flag is not None
    assert flag.pattern_name == "Silent After Deliverable"
    assert flag.severity == Severity.HIGH
    assert "10 business days ago" in flag.signal_evidence
    assert "no inbound triage activity since" in flag.signal_evidence
    assert "airtable_replica.tasks" in flag.data_sources
    assert "airtable_replica.projects" in flag.data_sources
    assert flag.confidence == 0.9


def test_inbound_after_deliverable_silences_flag() -> None:
    """If client has reached out post-delivery, no signal."""
    extras = {
        "last_completed_deliverable_at": datetime(2026, 4, 27, tzinfo=UTC),
        # Client emailed 2 days after delivery
        "last_inbound_at": datetime(2026, 4, 29, tzinfo=UTC),
    }
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(extras)) is None


def test_inbound_before_deliverable_still_fires() -> None:
    """An old inbound before the deliverable doesn't excuse silence after it."""
    extras = {
        "last_completed_deliverable_at": datetime(2026, 4, 27, tzinfo=UTC),
        "last_inbound_at": datetime(2026, 4, 20, tzinfo=UTC),  # before delivery
    }
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    flag = signal.evaluate(_state(extras))

    assert flag is not None
    assert "before the deliverable" in flag.signal_evidence


def test_iso_string_inputs_are_accepted() -> None:
    extras = {
        "last_completed_deliverable_at": "2026-04-27T00:00:00+00:00",
        "last_inbound_at": None,
    }
    signal = SilentAfterDeliverableSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(extras)) is not None
