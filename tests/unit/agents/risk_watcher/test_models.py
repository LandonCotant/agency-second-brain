"""Unit tests for the Risk Watcher dataclasses + ``Signal`` protocol."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from agency_brain.agents.risk_watcher.models import (
    ClientState,
    Flag,
    Profile,
    RiskWatcherInput,
    RiskWatcherOutput,
    Segment,
    Severity,
    Signal,
)


def _state(account_id: str = "recAcct1", segment: Segment = Segment.ECOMMERCE) -> ClientState:
    return ClientState(
        account_id=account_id,
        account_name="Test Co",
        segment=segment,
        project_id="recProj1",
    )


def test_severity_values_match_routing_matrix_keys() -> None:
    """The four severity strings are the keys WS-D's matrix expects.

    Hardcoded to catch any silent rename — a renamed enum value would
    break the routing matrix's `_MATRIX[severity]` lookup at runtime.
    """
    assert Severity.CRITICAL.value == "critical"
    assert Severity.HIGH.value == "high"
    assert Severity.MEDIUM.value == "medium"
    assert Severity.LOW.value == "low"


def test_segment_values_match_airtable_replica() -> None:
    """Profile-segment labels match `risk_profiles.segment` values."""
    assert Segment.ECOMMERCE.value == "E-commerce"
    assert Segment.LOCAL_SERVICE.value == "Local Service"
    assert Segment.AGENCY_PARTNER.value == "Agency Partner"


def test_client_state_is_frozen() -> None:
    """ClientState passes through Signals; mutating it would mask bugs."""
    state = _state()
    with pytest.raises(FrozenInstanceError):
        state.account_id = "other"  # type: ignore[misc]


def test_flag_is_frozen() -> None:
    flag = Flag(
        pattern_name="DummySignal",
        severity=Severity.HIGH,
        account_id="recAcct1",
        project_id=None,
        segment=Segment.ECOMMERCE,
        signal_evidence="evidence",
        reasoning="why",
        confidence=0.9,
    )
    with pytest.raises(FrozenInstanceError):
        flag.confidence = 0.5  # type: ignore[misc]


def test_signal_protocol_runtime_shape() -> None:
    """Anything with ``name`` + ``severity`` + ``evaluate`` satisfies the Protocol."""

    class GoodSignal:
        name = "good"
        severity = Severity.MEDIUM

        def evaluate(self, client_state: ClientState) -> Flag | None:
            return None

    instance = GoodSignal()
    # Static-typed Protocol — runtime structural check via attribute
    # access; this assertion documents the expected shape for PR-B.
    assert callable(instance.evaluate)
    assert instance.evaluate(_state()) is None
    # ``Signal`` is the importable Protocol — keeps the import alive
    # for downstream linters.
    assert Signal is not None


def test_profile_holds_signals_tuple() -> None:
    """Profile signals are an immutable tuple — accidental list-mutation
    of a profile shared across ticks would be a real footgun."""

    class DummySignal:
        name = "dummy"
        severity = Severity.LOW

        def evaluate(self, client_state: ClientState) -> Flag | None:
            return None

    profile = Profile(segment=Segment.ECOMMERCE, signals=(DummySignal(),))
    assert isinstance(profile.signals, tuple)
    assert profile.signals[0].name == "dummy"


def test_risk_watcher_output_quiet_tick_has_high_confidence() -> None:
    """Empty flags + confidence=1.0 keeps quiet ticks below the
    BaseAgent ``CONFIDENCE_THRESHOLD < 0.7`` human-review gate."""
    output = RiskWatcherOutput(
        flags=(),
        confidence=1.0,
        accounts_evaluated=3,
    )
    assert len(output.flags) == 0
    assert output.confidence == 1.0


def test_risk_watcher_input_carries_aspects_for_hipaa_guard() -> None:
    """``aspects`` is the BaseAgent HIPAA pre-flight key (ADR 0006)."""
    input_ = RiskWatcherInput(
        states=(_state(),),
        aspects=("hipaa_excluded",),
    )
    assert "hipaa_excluded" in input_.aspects
