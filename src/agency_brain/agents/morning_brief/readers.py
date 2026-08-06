"""BQ readers for the Morning Brief composer.

Pattern mirrors `triage/goal_context.py:GoalContextLoader`: each reader
takes a `BQQueryClient` Protocol (so unit tests stub it without touching
real BQ) and a `project_id`, exposes `load() -> list[<row dataclass>]`,
and returns immutable rows. The composer turns those into prompt
sections.

Per ADR 0029: HIPAA-flagged accounts are filtered upstream by the
WS-B Airtable sync's filterByFormula, so the readers never see HIPAA
data — `agent_outputs.triaged_items` is produced from the already-
filtered replica. The Risk Flags reader additionally scopes through
`account_owners_v` to resolve ownership.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .models import (
    DraftAwaitingReviewSnippet,
    OpenTaskSnippet,
    RiskFlagSnippet,
    TriagedItemSnippet,
)


class BQQueryClient(Protocol):
    """Minimal BQ surface — matches `goal_context.BQQueryClient`."""

    def query_rows(self, sql: str) -> list[dict]:
        """Execute SQL, return rows as a list of dicts."""
        ...


# ----------------------------------------------------- triaged items

_TRIAGED_ITEMS_SQL = """
SELECT
  ti.item_id,
  ti.severity,
  ti.reasoning AS summary,
  ti.source,
  ti.source_url
FROM `{project}.agent_outputs.triaged_items` AS ti
WHERE ti.triaged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND ti.actionable = TRUE
  AND (ti.owner_email = @recipient OR ti.owner_email IS NULL)
  AND ti.severity IN ('critical', 'high', 'medium')
ORDER BY
  CASE ti.severity
    WHEN 'critical' THEN 0
    WHEN 'high' THEN 1
    WHEN 'medium' THEN 2
    ELSE 3
  END,
  ti.confidence DESC,
  ti.triaged_at DESC
LIMIT @limit
"""


class TriagedItemsForOwnerReader:
    """Triaged Items from the last 24h owned by (or NULL-owned addressed to) the recipient."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 8,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, recipient_email: str) -> list[TriagedItemSnippet]:
        sql = _format_sql(
            _TRIAGED_ITEMS_SQL,
            project=self._project_id,
            recipient=recipient_email,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            TriagedItemSnippet(
                item_id=str(row["item_id"]),
                severity=str(row["severity"]),
                summary=str(row["summary"]),
                source=str(row["source"]),
                source_url=row.get("source_url"),
            )
            for row in rows
        ]


# ----------------------------------------------------- open tasks

# Tasks.Owner is a singleCollaborator with no `_extract` annotation, so the
# replica column holds the collaborator's *email* (not the usrXXX id — that
# extraction is opt-in per ADR 0019 and only Team.User uses it). Compare it
# to the recipient email directly; joining team.user against it never matches.
_OPEN_TASKS_SQL = """
SELECT
  t._airtable_record_id AS task_id,
  t.task_name AS name,
  t.due_date,
  p.project_name
FROM `{project}.airtable_replica.tasks` AS t
LEFT JOIN `{project}.airtable_replica.projects` AS p
  ON p._airtable_record_id = t.project[SAFE_OFFSET(0)]
WHERE COALESCE(t.status, '') != 'Done'
  AND LOWER(t.owner) = LOWER(@recipient)
ORDER BY
  CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END,
  t.due_date ASC
LIMIT @limit
"""


class OpenTasksForOwnerReader:
    """Open Airtable Tasks where the recipient is the assignee, sorted by Due Date."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 10,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, recipient_email: str) -> list[OpenTaskSnippet]:
        sql = _format_sql(
            _OPEN_TASKS_SQL,
            project=self._project_id,
            recipient=recipient_email,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            OpenTaskSnippet(
                task_id=str(row["task_id"]),
                name=str(row["name"]),
                due_date=_coerce_date(row.get("due_date")),
                project_name=row.get("project_name"),
            )
            for row in rows
        ]


# ----------------------------------------------------- risk flags

_RISK_FLAGS_SQL = """
SELECT
  rf.flag_id,
  rf.severity,
  rf.pattern_name,
  rf.reasoning,
  ao.account_name
FROM `{project}.agent_outputs.risk_flags` AS rf
LEFT JOIN `{project}.airtable_replica.account_owners_v` AS ao
  ON ao.account_id = rf.account_id
WHERE rf.flagged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND ao.owner_email = @recipient
  AND ao.hipaa = FALSE
ORDER BY rf.flagged_at DESC
LIMIT @limit
"""


class RiskFlagsReader:
    """Last-24h Risk Watcher flags scoped to one of the recipient's accounts.

    v1 returns an empty list because Risk Watcher hasn't shipped — but the
    SQL is in place so the section materializes the moment it does.
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 5,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, recipient_email: str) -> list[RiskFlagSnippet]:
        sql = _format_sql(
            _RISK_FLAGS_SQL,
            project=self._project_id,
            recipient=recipient_email,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            RiskFlagSnippet(
                flag_id=str(row["flag_id"]),
                severity=str(row["severity"]),
                pattern_name=str(row["pattern_name"]),
                account_name=row.get("account_name"),
                reasoning=row.get("reasoning"),
            )
            for row in rows
        ]


# ----------------------------------------------------- drafts awaiting review

_DRAFTS_AWAITING_SQL = """
SELECT
  t._airtable_record_id AS task_id,
  t.task_name AS name,
  p.project_name,
  t._airtable_last_modified AS drafted_at
FROM `{project}.airtable_replica.tasks` AS t
LEFT JOIN `{project}.airtable_replica.projects` AS p
  ON p._airtable_record_id = t.project[SAFE_OFFSET(0)]
WHERE t.approval_status = 'Drafted by Agent'
  AND COALESCE(t.status, '') != 'Done'
ORDER BY t._airtable_last_modified DESC
LIMIT @limit
"""


class DraftsAwaitingReviewReader:
    """Airtable Tasks marked as drafts awaiting human approval."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 5,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, recipient_email: str) -> list[DraftAwaitingReviewSnippet]:
        # recipient_email is unused at v1 — drafts are global. Future: filter
        # by Owner = recipient. The arg is here so the reader's signature
        # matches the others uniformly.
        del recipient_email
        sql = _format_sql(_DRAFTS_AWAITING_SQL, project=self._project_id, limit=self._limit)
        rows = self._bq.query_rows(sql)
        return [
            DraftAwaitingReviewSnippet(
                task_id=str(row["task_id"]),
                name=str(row["name"]),
                project_name=row.get("project_name"),
                drafted_at=row.get("drafted_at"),
            )
            for row in rows
        ]


# ----------------------------------------------------- helpers


def _format_sql(template: str, **kwargs: object) -> str:
    """Render a parameterized SQL template with literal values.

    The readers' callers pass scalar values (recipient email, limit) so we
    inline them via .format() rather than threading a parameterized
    QueryJobConfig through the BQQueryClient Protocol. SQL injection risk
    is low because all callers are internal (operator-controlled env vars
    + the writer's own dataclass). Email is single-quoted; integer limits
    are coerced via repr.
    """
    rendered = template.replace("{project}", str(kwargs["project"]))
    if "recipient" in kwargs:
        # Single-quote-escape: email addresses don't contain quotes, but be
        # explicit. Replace any embedded quote with a doubled quote.
        recipient = str(kwargs["recipient"]).replace("'", "''")
        rendered = rendered.replace("@recipient", f"'{recipient}'")
    if "limit" in kwargs:
        rendered = rendered.replace("@limit", str(int(kwargs["limit"])))
    return rendered


def _coerce_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None
