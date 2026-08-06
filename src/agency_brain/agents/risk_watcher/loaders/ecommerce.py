"""E-commerce client-state loader (ADR 0033 PR-B).

Builds one ``ClientState`` per active, non-HIPAA E-commerce account
with per-account rollups the e-commerce signals consume.
"""

from __future__ import annotations

from typing import Any

from ..models import ClientState, Segment
from .common import BQQueryClient, in_list


class EcommerceClientStateLoader:
    """Build one ``ClientState`` per active, non-HIPAA E-commerce account.

    ``extras`` populated:
    - ``pending_drafted_tasks``: oldest-first list of
      ``{record_id, task_name, created}`` for tasks under any of the
      account's projects that are still in approval_status =
      ``Drafted by Agent``.
    - ``last_completed_deliverable_at``: max ``completed_date`` across
      the account's projects' tasks where ``status = Done`` (or null).
    - ``last_inbound_at``: max ``ingested_at`` from
      ``agent_outputs.triaged_items`` where ``account_id`` matches.

    The HIPAA cascade keeps ``hipaa = true`` accounts out of the
    replica sync (per ADR 0020 + ``hipaa_filters.py``), so the loader
    doesn't need its own HIPAA filter — it just selects what it sees.
    """

    def __init__(self, *, bq: BQQueryClient, project_id: str) -> None:
        self._bq = bq
        self._project_id = project_id

    def load(self) -> tuple[ClientState, ...]:
        accounts = self._load_accounts()
        if not accounts:
            return ()

        pending = self._load_pending_drafted_tasks(account_ids=[a["account_id"] for a in accounts])
        deliverables = self._load_last_deliverables(account_ids=[a["account_id"] for a in accounts])
        inbound = self._load_last_inbound(account_ids=[a["account_id"] for a in accounts])

        states: list[ClientState] = []
        for a in accounts:
            account_id = a["account_id"]
            states.append(
                ClientState(
                    account_id=account_id,
                    account_name=a["company_name"],
                    segment=Segment.ECOMMERCE,
                    project_id=a.get("primary_project_id"),
                    aspects=(),
                    baseline={},
                    extras={
                        "pending_drafted_tasks": pending.get(account_id, []),
                        "last_completed_deliverable_at": deliverables.get(account_id),
                        "last_inbound_at": inbound.get(account_id),
                    },
                )
            )
        return tuple(states)

    # ----------------------------------------------------------- queries

    def _load_accounts(self) -> list[dict[str, Any]]:
        # De-correlated form: a CTE aggregates active projects per
        # account into an array, then the main query LEFT JOINs and
        # picks the lone project id when ARRAY_LENGTH = 1. BQ rejects
        # correlated subqueries that reference other tables (even when
        # the subquery is aggregable), so the previous ANY_VALUE-with-
        # HAVING form failed at runtime.
        sql = (
            "WITH active_projects AS ( "  # noqa: S608  project_id is config; no user input
            "  SELECT account[OFFSET(0)] AS account_id, "
            "         ARRAY_AGG(_airtable_record_id) AS project_ids "
            f"  FROM `{self._project_id}.airtable_replica.projects` "
            "  WHERE status = 'In Progress' "
            "  GROUP BY account_id "
            ") "
            "SELECT a._airtable_record_id AS account_id, "
            "  a.company_name, "
            "  IF(ARRAY_LENGTH(ap.project_ids) = 1, "
            "     ap.project_ids[OFFSET(0)], NULL) AS primary_project_id "
            f"FROM `{self._project_id}.airtable_replica.accounts` a "
            "LEFT JOIN active_projects ap "
            "  ON ap.account_id = a._airtable_record_id "
            "WHERE a.segment = 'E-commerce' "
            "  AND a.status IN ('Active', 'Mature') "
            "  AND a.hipaa = FALSE"
        )
        return self._bq.query_rows(sql)

    def _load_pending_drafted_tasks(
        self, *, account_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not account_ids:
            return {}
        sql = (
            "SELECT t._airtable_record_id AS record_id, "  # noqa: S608  account_ids are Airtable record ids; no user input
            "  t.task_name, t._airtable_last_modified AS created, "
            "  p.account[OFFSET(0)] AS account_id "
            f"FROM `{self._project_id}.airtable_replica.tasks` t "
            f"JOIN `{self._project_id}.airtable_replica.projects` p "
            "  ON t.project[OFFSET(0)] = p._airtable_record_id "
            "WHERE t.approval_status = 'Drafted by Agent' "
            f"  AND p.account[OFFSET(0)] IN ({in_list(account_ids)}) "
            "ORDER BY t._airtable_last_modified ASC"
        )
        out: dict[str, list[dict[str, Any]]] = {a: [] for a in account_ids}
        for r in self._bq.query_rows(sql):
            out.setdefault(r["account_id"], []).append(
                {
                    "record_id": r["record_id"],
                    "task_name": r["task_name"],
                    "created": r["created"],
                }
            )
        return out

    def _load_last_deliverables(self, *, account_ids: list[str]) -> dict[str, Any]:
        if not account_ids:
            return {}
        sql = (
            "SELECT p.account[OFFSET(0)] AS account_id, "  # noqa: S608  account_ids are Airtable record ids; no user input
            "  MAX(t.completed_date) AS last_completed_at "
            f"FROM `{self._project_id}.airtable_replica.tasks` t "
            f"JOIN `{self._project_id}.airtable_replica.projects` p "
            "  ON t.project[OFFSET(0)] = p._airtable_record_id "
            "WHERE t.status = 'Done' "
            "  AND t.completed_date IS NOT NULL "
            f"  AND p.account[OFFSET(0)] IN ({in_list(account_ids)}) "
            "GROUP BY account_id"
        )
        out: dict[str, Any] = {}
        for r in self._bq.query_rows(sql):
            out[r["account_id"]] = r["last_completed_at"]
        return out

    def _load_last_inbound(self, *, account_ids: list[str]) -> dict[str, Any]:
        """Last inbound triage activity per account.

        ``agent_outputs.triaged_items`` doesn't carry a resolved
        ``account_id`` column directly — the resolution chain
        (``Contact.email → Account → active Project``, per ADR 0022)
        terminates in ``airtable_task_record_id``, which links to a
        Tasks row whose project links to an account.

        Cheaper proxy: ``airtable_replica.tasks`` rows where
        ``source = 'Triage Agent'`` ARE the inbound-signal artifacts.
        Their ``_airtable_last_modified`` (= Airtable record
        ``createdTime``) is the inbound time. Same join we
        already do for pending drafted tasks; no extra dataset hop.
        """
        if not account_ids:
            return {}
        sql = (
            "SELECT p.account[OFFSET(0)] AS account_id, "  # noqa: S608  account_ids are Airtable record ids; no user input
            "  MAX(t._airtable_last_modified) AS last_inbound_at "
            f"FROM `{self._project_id}.airtable_replica.tasks` t "
            f"JOIN `{self._project_id}.airtable_replica.projects` p "
            "  ON t.project[OFFSET(0)] = p._airtable_record_id "
            "WHERE t.source = 'Triage Agent' "
            f"  AND p.account[OFFSET(0)] IN ({in_list(account_ids)}) "
            "GROUP BY account_id"
        )
        out: dict[str, Any] = {}
        for r in self._bq.query_rows(sql):
            out[r["account_id"]] = r["last_inbound_at"]
        return out
