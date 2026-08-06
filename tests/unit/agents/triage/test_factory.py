"""Unit tests for the shared Triage Agent factory (ADR 0061).

build_triage_agent wires the real goal/owner context loaders against BigQuery
and assembles the TriageAgent. This is the wiring that previously lived in
agent.py:TriageQueryAgent.set_up() (moved here so the bridge can share it).
"""

from __future__ import annotations

from typing import Any

from agency_brain.agents.triage import factory
from agency_brain.agents.triage.goal_context import (
    AccountOwnersContextLoader,
    GoalContextLoader,
)


class _FakeQueryJob:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def result(self) -> list[dict]:
        return self._rows


class _FakeBQClient:
    def __init__(self, *, project: str) -> None:
        self.project = project
        self.queries: list[str] = []

    def query(self, sql: str) -> _FakeQueryJob:
        self.queries.append(sql)
        if "airtable_replica.goals" in sql:
            return _FakeQueryJob(
                [
                    {
                        "goal_id": "G-2026Q2-01",
                        "name": "Land 2 e-commerce retainers",
                        "horizon": "Quarterly",
                        "status": "Active",
                        "parent_goal_id": None,
                    }
                ]
            )
        if "airtable_replica.account_owners_v" in sql:
            return _FakeQueryJob(
                [
                    {
                        "account_id": "recAcc01",
                        "project_id": "recProj01",
                        "owner_email": "owner@example.com",
                        "hipaa": False,
                    }
                ]
            )
        return _FakeQueryJob([])


class _RecordingTriageAgent:
    instances: list[_RecordingTriageAgent] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.__class__.instances.append(self)


class _FakeVertexClassifier:
    def __init__(self, *, config: Any) -> None:
        self.config = config


def test_build_triage_agent_wires_real_goal_and_owner_context_loaders(monkeypatch) -> None:
    monkeypatch.setattr(factory, "TriageAgent", _RecordingTriageAgent)
    monkeypatch.setattr(factory, "VertexClassifier", _FakeVertexClassifier)
    _RecordingTriageAgent.instances = []

    # Inject the fake BQ client + audit/memory so no real GCP clients are built;
    # the Airtable writer leg stays disabled (its env vars are unset).
    factory.build_triage_agent(
        project_id="agency-brain-demo",
        bq_client=_FakeBQClient(project="agency-brain-demo"),
        audit_log=object(),
        memory_bank=object(),
    )

    created = _RecordingTriageAgent.instances[0]
    goal_context = created.kwargs["goal_context"]
    owners_context = created.kwargs["owners_context"]

    assert isinstance(goal_context, GoalContextLoader)
    assert isinstance(owners_context, AccountOwnersContextLoader)
    assert "G-2026Q2-01: Land 2 e-commerce retainers (Quarterly)" in (goal_context.text_block())
    assert "recAcc01 / project recProj01 / owner owner@example.com" in (owners_context.text_block())
    # No Airtable writer leg without its env vars (classify-only build).
    assert created.kwargs["task_drafter"] is None
    assert created.kwargs["items_writer"] is None
