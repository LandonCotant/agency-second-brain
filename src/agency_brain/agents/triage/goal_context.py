"""Goal + client/owner context loaders for the Triage prompt.

PR 2: direct BQ reads. PR 4 wraps `GoalContextLoader` with Vertex AI
Cached Contents (5-min TTL per PRD §6.3) so the prompt can reuse the
goal block across many invocations within the cache window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BQQueryClient(Protocol):
    """Minimal BQ surface — matches `google.cloud.bigquery.Client.query`'s
    `result()` rows iterator pattern."""

    def query_rows(self, sql: str) -> list[dict]:
        """Execute SQL, return rows as a list of dicts."""
        ...


# ------------------------------------------------------------- goals


@dataclass(frozen=True)
class GoalRow:
    goal_id: str
    name: str
    horizon: str
    status: str
    parent_goal_id: str | None = None


_GOALS_SQL = """
SELECT
  _airtable_record_id AS goal_id,
  goal_name AS name,
  horizon,
  status,
  parent_goal[SAFE_OFFSET(0)] AS parent_goal_id
FROM `{project}.airtable_replica.goals`
WHERE status = 'Active'
  AND horizon IN ('Quarterly', '1-year')
ORDER BY horizon, name
"""


class GoalContextLoader:
    """Loads active goals at `Quarterly`/`1-year` horizons from BQ.

    Renders a compact text block that the prompt template substitutes into
    `{{goal_context}}`. Format: one line per goal, `goal_id: name (horizon)`.
    """

    def __init__(self, *, bq_client: BQQueryClient, project_id: str) -> None:
        self._bq = bq_client
        self._project_id = project_id

    def load(self) -> list[GoalRow]:
        sql = _GOALS_SQL.format(project=self._project_id)
        rows = self._bq.query_rows(sql)
        return [
            GoalRow(
                goal_id=str(row["goal_id"]),
                name=str(row["name"]),
                horizon=str(row["horizon"]),
                status=str(row["status"]),
                parent_goal_id=row.get("parent_goal_id"),
            )
            for row in rows
        ]

    def text_block(self) -> str:
        rows = self.load()
        if not rows:
            return "(no active goals at Quarterly or 1-year horizons)"
        lines = [f"{r.goal_id}: {r.name} ({r.horizon})" for r in rows]
        return "\n".join(lines)


# ------------------------------------------------------------- account owners


@dataclass(frozen=True)
class AccountOwnerRow:
    account_id: str
    project_id: str
    owner_email: str
    hipaa: bool


_ACCOUNT_OWNERS_SQL = """
SELECT
  account_id,
  project_id,
  owner_email,
  hipaa
FROM `{project}.airtable_replica.account_owners_v`
WHERE hipaa = FALSE
ORDER BY account_id
"""


class AccountOwnersContextLoader:
    """Loads non-HIPAA account/project/owner triples from the materialized view.

    Single-base architecture (ADR 0020). Renamed from ClientOwnersContextLoader.
    """

    def __init__(self, *, bq_client: BQQueryClient, project_id: str) -> None:
        self._bq = bq_client
        self._project_id = project_id

    def load(self) -> list[AccountOwnerRow]:
        sql = _ACCOUNT_OWNERS_SQL.format(project=self._project_id)
        rows = self._bq.query_rows(sql)
        return [
            AccountOwnerRow(
                account_id=str(row["account_id"]),
                project_id=str(row["project_id"]),
                owner_email=str(row["owner_email"]),
                hipaa=bool(row["hipaa"]),
            )
            for row in rows
        ]

    def text_block(self) -> str:
        rows = self.load()
        if not rows:
            return "(no non-HIPAA account/project/owner mappings yet)"
        lines = [f"{r.account_id} / project {r.project_id} / owner {r.owner_email}" for r in rows]
        return "\n".join(lines)
