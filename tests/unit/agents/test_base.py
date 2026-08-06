from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from agency_brain.agents.base import (
    BaseAgent,
    HipaaGuardTripped,
)
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank


@dataclass
class _Input:
    aspects: list[str] = field(default_factory=list)
    payload: str = ""


@dataclass
class _Output:
    confidence: float
    answer: str = ""


class _RecordingBQ:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.rows.extend(rows)
        return []


class _Agent(BaseAgent[_Input, _Output]):
    def __init__(self, *, behaviour, **kwargs) -> None:
        super().__init__(**kwargs)
        self._behaviour = behaviour
        self.run_called = 0

    def _run(self, input: _Input) -> _Output:
        self.run_called += 1
        return self._behaviour(input)


def _make_agent(behaviour) -> tuple[_Agent, _RecordingBQ]:
    bq = _RecordingBQ()
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    agent = _Agent(
        behaviour=behaviour,
        agent_id="triage",
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid="agent-id-uuid-abc",
    )
    return agent, bq


def test_successful_invocation_emits_passed_audit_row() -> None:
    agent, bq = _make_agent(lambda _i: _Output(confidence=0.91, answer="ok"))

    out = agent.invoke(_Input(aspects=["client", "project"]))

    assert out.answer == "ok"
    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row["agent_id"] == "triage"
    assert row["agent_identity_uuid"] == "agent-id-uuid-abc"
    assert row["hipaa_guard_status"] == "PASSED"
    assert row["success"] is True
    assert row["confidence"] == pytest.approx(0.91)
    assert row["human_review_routed"] is False
    assert row["error"] is None


def test_run_exception_emits_failure_row_and_reraises() -> None:
    def boom(_i: _Input) -> _Output:
        raise RuntimeError("downstream timeout")

    agent, bq = _make_agent(boom)

    with pytest.raises(RuntimeError, match="downstream timeout"):
        agent.invoke(_Input(aspects=["client"]))

    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row["success"] is False
    assert row["hipaa_guard_status"] == "PASSED"
    assert row["error"] == "RuntimeError: downstream timeout"
    assert row["confidence"] is None
    assert row["output"] is None


def test_hipaa_aspect_trips_guard_and_skips_run() -> None:
    agent, bq = _make_agent(lambda _i: _Output(confidence=1.0))

    with pytest.raises(HipaaGuardTripped):
        agent.invoke(_Input(aspects=["client", "hipaa_excluded"]))

    assert agent.run_called == 0
    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row["hipaa_guard_status"] == "TRIPPED"
    assert row["success"] is False
    assert "hipaa_excluded" in (row["error"] or "")


def test_low_confidence_routes_to_human_review() -> None:
    agent, bq = _make_agent(lambda _i: _Output(confidence=0.6, answer="maybe"))

    out = agent.invoke(_Input())

    assert out.answer == "maybe"
    row = bq.rows[0]
    assert row["success"] is True
    assert row["human_review_routed"] is True
    assert row["confidence"] == pytest.approx(0.6)


def test_confidence_at_threshold_does_not_route() -> None:
    """Boundary: confidence == 0.7 is the cutoff. < 0.7 routes; == 0.7 does not."""
    agent, bq = _make_agent(lambda _i: _Output(confidence=0.7))

    agent.invoke(_Input())

    assert bq.rows[0]["human_review_routed"] is False


def test_memory_bank_helpers_use_agent_id_namespace() -> None:
    agent, _ = _make_agent(lambda _i: _Output(confidence=1.0))

    agent._mb_write("client-123", "baseline", {"roas": 3.2})
    assert agent._mb_read("client-123", "baseline") == {"roas": 3.2}
    # Different entity_id is isolated.
    assert agent._mb_read("client-999", "baseline") is None
