"""Unit tests for the goal + account/owner BQ context loaders."""

from __future__ import annotations

from agency_brain.agents.triage.goal_context import (
    AccountOwnersContextLoader,
    GoalContextLoader,
)


class _StubBQ:
    def __init__(self, rows: list[dict]) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def query_rows(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        return self._rows


def test_goal_loader_renders_block_and_filters_by_horizon_in_sql() -> None:
    bq = _StubBQ(
        [
            {
                "goal_id": "G-2026Q2-01",
                "name": "Land 2 e-commerce retainers",
                "horizon": "Quarterly",
                "status": "Active",
                "parent_goal_id": "G-2026A-01",
            },
            {
                "goal_id": "G-2026A-01",
                "name": "$40k MRR",
                "horizon": "1-year",
                "status": "Active",
                "parent_goal_id": None,
            },
        ]
    )
    loader = GoalContextLoader(bq_client=bq, project_id="agency-brain-demo")
    block = loader.text_block()
    assert "G-2026Q2-01: Land 2 e-commerce retainers (Quarterly)" in block
    assert "G-2026A-01: $40k MRR (1-year)" in block
    # SQL filters happen at the warehouse, not in code:
    sql = bq.queries[0]
    assert "status = 'Active'" in sql
    assert "horizon IN ('Quarterly', '1-year')" in sql
    assert "_airtable_record_id AS goal_id" in sql
    assert "goal_name AS name" in sql
    assert "parent_goal[SAFE_OFFSET(0)] AS parent_goal_id" in sql
    assert "agency-brain-demo.airtable_replica.goals" in sql


def test_goal_loader_returns_explanatory_text_when_no_active_goals() -> None:
    loader = GoalContextLoader(bq_client=_StubBQ([]), project_id="p")
    block = loader.text_block()
    assert "no active goals" in block.lower()


def test_account_owners_loader_filters_hipaa_in_sql() -> None:
    bq = _StubBQ(
        [
            {
                "account_id": "recAcc01",
                "project_id": "recProj01",
                "owner_email": "owner@example.com",
                "hipaa": False,
            }
        ]
    )
    loader = AccountOwnersContextLoader(bq_client=bq, project_id="agency-brain-demo")
    block = loader.text_block()
    assert "recAcc01" in block
    assert "recProj01" in block
    assert "owner@example.com" in block
    sql = bq.queries[0]
    assert "hipaa = FALSE" in sql
    assert "account_owners_v" in sql


def test_account_owners_loader_returns_explanatory_text_when_view_empty() -> None:
    loader = AccountOwnersContextLoader(bq_client=_StubBQ([]), project_id="p")
    block = loader.text_block()
    assert "no non-hipaa" in block.lower()
