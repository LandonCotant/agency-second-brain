"""Unit tests for ``AcknowledgmentGapSignal``."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Segment,
    Severity,
)
from agency_brain.agents.risk_watcher.signals import AcknowledgmentGapSignal


def _state(pending: list[dict] | None) -> ClientState:
    return ClientState(
        account_id="recAcct1",
        account_name="Acme Co",
        segment=Segment.ECOMMERCE,
        project_id="recProj1",
        extras={"pending_drafted_tasks": pending or []},
    )


def _now() -> datetime:
    # 2026-05-11 is a Monday.
    return datetime(2026, 5, 11, 12, tzinfo=UTC)


def test_no_pending_tasks_returns_none() -> None:
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(pending=None)) is None
    assert signal.evaluate(_state(pending=[])) is None


def test_pending_under_threshold_returns_none() -> None:
    """A 2-business-day gap (Thu created → Mon now) doesn't fire at threshold=5."""
    pending = [
        {
            "record_id": "recT1",
            "task_name": "Recent task",
            "created": datetime(2026, 5, 7, 9, tzinfo=UTC),  # Thu
        }
    ]
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    assert signal.evaluate(_state(pending=pending)) is None


def test_pending_at_or_above_threshold_fires() -> None:
    """Mon two weeks ago → today = 10 business days, well over 5."""
    pending = [
        {
            "record_id": "recT1",
            "task_name": "Old task",
            "created": datetime(2026, 4, 27, 9, tzinfo=UTC),  # Mon two weeks ago
        }
    ]
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    flag = signal.evaluate(_state(pending=pending))

    assert flag is not None
    assert flag.pattern_name == "Acknowledgment Gap"
    assert flag.severity == Severity.HIGH
    assert flag.account_id == "recAcct1"
    assert flag.segment == Segment.ECOMMERCE
    assert flag.confidence == 0.95
    assert "1 drafted task(s) overdue" in flag.signal_evidence
    assert "10 business days" in flag.signal_evidence
    assert "'Old task'" in flag.signal_evidence
    assert flag.data_sources == ("airtable_replica.tasks",)


def test_evidence_counts_overdue_tasks_only() -> None:
    """Three tasks but only two cross threshold => evidence reports 2 overdue."""
    pending = [
        {
            "record_id": "recT_old",
            "task_name": "Old",
            "created": datetime(2026, 4, 27, 9, tzinfo=UTC),  # 10 bdays
        },
        {
            "record_id": "recT_old2",
            "task_name": "Older",
            "created": datetime(2026, 4, 28, 9, tzinfo=UTC),  # 9 bdays
        },
        {
            "record_id": "recT_recent",
            "task_name": "Recent",
            "created": datetime(2026, 5, 7, 9, tzinfo=UTC),  # 2 bdays
        },
    ]
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    flag = signal.evaluate(_state(pending=pending))

    assert flag is not None
    assert "2 drafted task(s) overdue" in flag.signal_evidence


def test_iso_string_created_is_accepted() -> None:
    """Loader can pass either datetime or ISO string."""
    pending = [
        {
            "record_id": "recT1",
            "task_name": "Old",
            "created": "2026-04-27T09:00:00+00:00",
        }
    ]
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    flag = signal.evaluate(_state(pending=pending))
    assert flag is not None


def test_pattern_name_segment_severity_inject_into_emitted_flag() -> None:
    """ADR 0034 §2: cross-segment reuse via constructor params.

    Local Service's "Approval Slowdown" reuses this class with custom
    labels rather than subclassing.
    """
    pending = [
        {
            "record_id": "recT1",
            "task_name": "Old",
            "created": datetime(2026, 4, 27, 9, tzinfo=UTC),  # 10 bdays old
        }
    ]
    ls_state = ClientState(
        account_id="recAcctLS",
        account_name="Client A",
        segment=Segment.LOCAL_SERVICE,
        project_id="recProjLS",
        extras={"pending_drafted_tasks": pending},
    )
    signal = AcknowledgmentGapSignal(
        business_days_threshold=5,
        pattern_name="Approval Slowdown",
        segment=Segment.LOCAL_SERVICE,
        severity=Severity.HIGH,
        now_fn=_now,
    )

    flag = signal.evaluate(ls_state)

    assert flag is not None
    assert flag.pattern_name == "Approval Slowdown"
    assert flag.segment == Segment.LOCAL_SERVICE
    assert flag.severity == Severity.HIGH
    # Reasoning text reflects the configured labels (so a Chat card
    # reads correctly for the segment).
    assert "Local Service" in flag.reasoning
    assert "'Approval Slowdown'" in flag.reasoning


def test_default_construction_preserves_ecommerce_labels() -> None:
    """Existing PR-B call sites that don't pass the new kwargs keep working."""
    signal = AcknowledgmentGapSignal(business_days_threshold=5, now_fn=_now)
    assert signal.name == "Acknowledgment Gap"
    assert signal.segment == Segment.ECOMMERCE
    assert signal.severity == Severity.HIGH
