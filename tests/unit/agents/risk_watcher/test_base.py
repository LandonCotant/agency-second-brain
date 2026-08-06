"""Unit tests for ``RiskWatcher`` — the agent's evaluate-and-aggregate loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from agency_brain.agents.base import HipaaGuardTripped
from agency_brain.agents.risk_watcher.base import (
    BASELINE_KEY,
    RiskWatcher,
)
from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Flag,
    Profile,
    RiskWatcherInput,
    Segment,
    Severity,
)
from agency_brain.common.memory_bank import InMemoryMemoryBank

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _CapturingAuditLog:
    captured: list = field(default_factory=list)

    def emit(self, event: Any) -> None:
        self.captured.append(event)


@dataclass
class _StubSignal:
    """Returns whichever Flag-or-None the test seeded."""

    name: str
    severity: Severity
    response: Flag | None = None
    calls: list[ClientState] = field(default_factory=list)

    def evaluate(self, client_state: ClientState) -> Flag | None:
        self.calls.append(client_state)
        return self.response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(account_id: str = "recAcct1") -> ClientState:
    return ClientState(
        account_id=account_id,
        account_name="Test Co",
        segment=Segment.ECOMMERCE,
        project_id="recProj1",
    )


def _watcher(profile: Profile) -> tuple[RiskWatcher, _CapturingAuditLog, InMemoryMemoryBank]:
    audit = _CapturingAuditLog()
    mb = InMemoryMemoryBank()
    watcher = RiskWatcher(
        agent_id="risk-watcher",
        sa_email="asb-risk-watcher-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=mb,
        profile=profile,
    )
    return watcher, audit, mb


def _make_flag(account_id: str, name: str, confidence: float) -> Flag:
    return Flag(
        pattern_name=name,
        severity=Severity.HIGH,
        account_id=account_id,
        project_id=None,
        segment=Segment.ECOMMERCE,
        signal_evidence="evidence",
        reasoning="because",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Tests — evaluate loop
# ---------------------------------------------------------------------------


def test_quiet_tick_returns_high_confidence_no_flags() -> None:
    """No signals fire = output.confidence == 1.0 (quiet ticks pass
    BaseAgent's < 0.7 human-review gate)."""
    profile = Profile(
        segment=Segment.ECOMMERCE,
        signals=(_StubSignal(name="quiet", severity=Severity.LOW, response=None),),
    )
    watcher, audit, _ = _watcher(profile)

    output = watcher.invoke(RiskWatcherInput(states=(_state(),)))

    assert output.flags == ()
    assert output.confidence == 1.0
    assert output.accounts_evaluated == 1
    assert len(audit.captured) == 1
    assert audit.captured[0].success is True


def test_one_signal_one_state_one_flag() -> None:
    fired = _make_flag(account_id="recAcct1", name="GapDetected", confidence=0.92)
    profile = Profile(
        segment=Segment.ECOMMERCE,
        signals=(_StubSignal(name="gap", severity=Severity.HIGH, response=fired),),
    )
    watcher, _, _ = _watcher(profile)

    output = watcher.invoke(RiskWatcherInput(states=(_state(),)))

    assert len(output.flags) == 1
    assert output.flags[0].pattern_name == "GapDetected"
    assert output.confidence == pytest.approx(0.92)


def test_multiple_signals_all_fire_min_confidence_wins() -> None:
    """Aggregate confidence is the MIN of fired flags — pessimistic."""
    f1 = _make_flag("recAcct1", "Gap", 0.95)
    f2 = _make_flag("recAcct1", "Silence", 0.71)
    profile = Profile(
        segment=Segment.ECOMMERCE,
        signals=(
            _StubSignal(name="gap", severity=Severity.HIGH, response=f1),
            _StubSignal(name="silence", severity=Severity.MEDIUM, response=f2),
        ),
    )
    watcher, _, _ = _watcher(profile)

    output = watcher.invoke(RiskWatcherInput(states=(_state(),)))

    assert len(output.flags) == 2
    assert output.confidence == pytest.approx(0.71)


def test_multiple_states_each_signal_evaluated_per_state() -> None:
    """Cross-product: 2 states x 2 signals = 4 evaluate calls."""
    sig_a = _StubSignal(name="a", severity=Severity.HIGH, response=None)
    sig_b = _StubSignal(name="b", severity=Severity.HIGH, response=None)
    profile = Profile(segment=Segment.ECOMMERCE, signals=(sig_a, sig_b))
    watcher, _, _ = _watcher(profile)

    states = (_state("recAcct1"), _state("recAcct2"))
    output = watcher.invoke(RiskWatcherInput(states=states))

    assert len(sig_a.calls) == 2
    assert len(sig_b.calls) == 2
    assert output.accounts_evaluated == 2


# ---------------------------------------------------------------------------
# Tests — BaseAgent contract
# ---------------------------------------------------------------------------


def test_hipaa_excluded_aspect_short_circuits_with_audit_row() -> None:
    """ADR 0006 + agents/base.py:60 — hipaa_excluded raises and emits."""
    profile = Profile(
        segment=Segment.ECOMMERCE,
        signals=(_StubSignal(name="x", severity=Severity.HIGH),),
    )
    watcher, audit, _ = _watcher(profile)

    with pytest.raises(HipaaGuardTripped):
        watcher.invoke(RiskWatcherInput(states=(_state(),), aspects=("hipaa_excluded",)))

    assert len(audit.captured) == 1
    assert audit.captured[0].hipaa_guard_status.value == "TRIPPED"
    assert audit.captured[0].success is False


def test_input_summary_surfaces_count_and_segments_only() -> None:
    """Audit row carries shape, not account names (PRD §4.6)."""
    profile = Profile(segment=Segment.ECOMMERCE, signals=())
    watcher, audit, _ = _watcher(profile)

    watcher.invoke(
        RiskWatcherInput(
            states=(
                _state("recAcct1"),
                ClientState(
                    account_id="recAcct2",
                    account_name="Other",
                    segment=Segment.LOCAL_SERVICE,
                    project_id=None,
                ),
            )
        )
    )

    summary = audit.captured[0].input_summary
    assert summary is not None
    assert "states=2" in summary
    assert "E-commerce" in summary
    assert "Local Service" in summary
    # Account names should NOT leak into the audit summary.
    assert "Test Co" not in summary
    assert "Other" not in summary


# ---------------------------------------------------------------------------
# Tests — Memory Bank baseline helpers
# ---------------------------------------------------------------------------


def test_baseline_round_trip_uses_documented_namespace() -> None:
    """Namespace per ADR 0033 §3: risk-watcher/{account_id_lower}/baseline.

    Airtable record ids are mixed-case base62; the Memory Bank
    namespace builder rejects uppercase (test_memory_bank.py:28), so
    we lowercase at the risk-watcher boundary.
    """
    profile = Profile(segment=Segment.ECOMMERCE, signals=())
    watcher, _, mb = _watcher(profile)

    watcher.write_baseline("recAcct1", {"avg_response_hours": 4.5})
    out = watcher.read_baseline("recAcct1")

    assert out == {"avg_response_hours": 4.5}
    assert mb.read("risk-watcher/recacct1/baseline", BASELINE_KEY) == {"avg_response_hours": 4.5}


def test_baseline_returns_none_when_unset() -> None:
    profile = Profile(segment=Segment.ECOMMERCE, signals=())
    watcher, _, _ = _watcher(profile)

    assert watcher.read_baseline("recAcctNew") is None


# ---------------------------------------------------------------------------
# Tests — flag materialization
# ---------------------------------------------------------------------------


def test_materialize_flag_row_fills_managed_fields() -> None:
    """Writer-facing helper fills agent-managed columns; subclass-emitted
    Flag carries only the domain content."""
    flag = _make_flag("recAcct1", "Gap", 0.92)

    row = RiskWatcher.materialize_flag_row(flag, model="gemini-2.5-flash", prompt_version="v1")

    # Required-by-schema agent-side columns
    assert row.get("flag_id")
    assert row.get("flagged_at")
    assert row.get("agent_run_id")
    assert row["account_id"] == "recAcct1"
    assert row["pattern_name"] == "Gap"
    assert row["severity"] == "high"
    assert row["confidence"] == pytest.approx(0.92)
    assert row["model"] == "gemini-2.5-flash"
    assert row["prompt_version"] == "v1"
    # Confidence above threshold => not auto-routed to human review
    assert row["human_review_routed"] is False
    # Repeated field is materialized as list (BQ JSON insert expectation)
    assert row["data_sources"] == []


def test_materialize_flag_row_routes_low_confidence_to_human_review() -> None:
    flag = _make_flag("recAcct1", "Edge", 0.5)
    row = RiskWatcher.materialize_flag_row(flag, model="gemini-2.5-flash", prompt_version="v1")
    assert row["human_review_routed"] is True
