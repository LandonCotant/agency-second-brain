"""Local Service client-state loader (ADR 0034 PR-D, ADR 0035).

Builds one ``ClientState`` per active, non-HIPAA Local Service
account that ALSO has an active contract AND at least one CRM
contact with an email. Accounts failing either gate are dropped
entirely — Owner Disengagement can't exist for them and Approval
Slowdown alone doesn't justify their inclusion in this loader.

ADR 0035 reshaped the engagement model. v1 used calendar-only
recency keyed by attendee email domain (false-fired on free-mail).
v2 takes the MAX across:

- ``last_calendar_event_with_crm_attendee`` — Calendar event whose
  attendees include any of the account's CRM contact emails
  (exact-email match; no domain widening).
- ``last_triage_inbound_at`` — ``MAX(tasks._airtable_last_modified)``
  for tasks whose ``source = 'Triage Agent'`` linked to this account's
  projects (proxy for inbound emails the Triage pipeline classified).
- ``last_approved_or_done_task_at`` —
  ``MAX(tasks._airtable_last_modified)`` for tasks where
  ``approval_status = 'Approved' OR status = 'Done'`` (owner-side
  activity; pending Triage drafts don't count).

  NB: ``_airtable_last_modified`` is the canonical record-timestamp
  system column. Post-F9 (commit 4356c94) it carries the Airtable
  record ``createdTime`` (no Last Modified field is synced), so this
  approximates create-time rather than true edit-time. Best available
  and far better than the prior dead ``created``/``last_modified``
  columns, which 400'd every query once F9 dropped them.

Calendar reads are made through an injected ``CalendarClient`` so
tests stub the Calendar API. A failed Calendar call sets the
calendar source to None; the other two sources still contribute.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..models import ClientState, Segment, utc_now
from .common import BQQueryClient, in_list

log = logging.getLogger("agency_brain.agents.risk_watcher.loaders.local_service")

DEFAULT_ENGAGEMENT_LOOKBACK_DAYS = 30

# Source labels surfaced into ``ClientState.extras["winning_source"]``
# for the OwnerDisengagementSignal to embed in evidence text.
SOURCE_CALENDAR = "calendar"
SOURCE_TRIAGE_INBOUND = "triage inbound"
SOURCE_TASK_APPROVAL = "task approval/completion"


class CalendarRecencyClient(Protocol):
    """Subset of ``calendar_client.CalendarClient`` we depend on."""

    def most_recent_engagement_event(
        self,
        *,
        owner_email: str,
        attendee_emails: tuple[str, ...],
        since: datetime,
        until: datetime,
    ) -> datetime | None: ...


class LocalServiceClientStateLoader:
    """Build one ``ClientState`` per active LS account passing the gates."""

    def __init__(
        self,
        *,
        bq: BQQueryClient,
        project_id: str,
        calendar: CalendarRecencyClient | None = None,
        engagement_lookback_days: int = DEFAULT_ENGAGEMENT_LOOKBACK_DAYS,
        now_fn: type[utc_now] | None = None,
    ) -> None:
        self._bq = bq
        self._project_id = project_id
        self._calendar = calendar
        self._lookback_days = engagement_lookback_days
        self._now_fn = now_fn or utc_now

    def load(self) -> tuple[ClientState, ...]:
        accounts = self._load_accounts()
        if not accounts:
            return ()

        account_ids = [a["account_id"] for a in accounts]

        pending = self._load_pending_drafted_tasks(account_ids=account_ids)
        contact_emails = self._load_contact_emails(account_ids=account_ids)
        last_triage = self._load_last_triage_inbound(account_ids=account_ids)
        last_task_action = self._load_last_approved_or_done_task(account_ids=account_ids)

        now = self._now_fn()  # type: ignore[operator]
        since = now - timedelta(days=self._lookback_days)

        states: list[ClientState] = []
        for a in accounts:
            account_id = a["account_id"]
            owner_email = a.get("account_manager") or None
            emails = contact_emails.get(account_id, ())

            calendar_at = self._calendar_recency(
                owner_email=owner_email,
                attendee_emails=emails,
                since=since,
                until=now,
            )
            triage_at = _coerce_dt(last_triage.get(account_id))
            task_at = _coerce_dt(last_task_action.get(account_id))

            last_engagement_at, winning_source = _max_with_source(
                (SOURCE_CALENDAR, calendar_at),
                (SOURCE_TRIAGE_INBOUND, triage_at),
                (SOURCE_TASK_APPROVAL, task_at),
            )

            states.append(
                ClientState(
                    account_id=account_id,
                    account_name=a["company_name"],
                    segment=Segment.LOCAL_SERVICE,
                    project_id=a.get("primary_project_id"),
                    aspects=(),
                    baseline={},
                    extras={
                        "pending_drafted_tasks": pending.get(account_id, []),
                        "contact_emails": emails,
                        "owner_email": owner_email,
                        "last_engagement_at": last_engagement_at,
                        "engagement_lookback_days": self._lookback_days,
                        "winning_source": winning_source,
                    },
                )
            )
        return tuple(states)

    # --------------------------------------------------- queries

    def _load_accounts(self) -> list[dict[str, Any]]:
        # ADR 0035 §2: gate on (active contract) AND (has CRM contact
        # with email). Accounts failing either are dropped here.
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
            "  a.account_manager, "
            "  IF(ARRAY_LENGTH(ap.project_ids) = 1, "
            "     ap.project_ids[OFFSET(0)], NULL) AS primary_project_id "
            f"FROM `{self._project_id}.airtable_replica.accounts` a "
            "LEFT JOIN active_projects ap "
            "  ON ap.account_id = a._airtable_record_id "
            "WHERE a.segment = 'Local Service' "
            "  AND a.status IN ('Active', 'Mature') "
            "  AND a.hipaa = FALSE "
            "  AND EXISTS ( "
            f"    SELECT 1 FROM `{self._project_id}.airtable_replica.contracts` c "
            "    WHERE c.account[OFFSET(0)] = a._airtable_record_id "
            "      AND c.contract_status = 'Active' "
            "      AND (c.expiry_date IS NULL OR c.expiry_date >= CURRENT_DATE()) "
            "  ) "
            "  AND EXISTS ( "
            f"    SELECT 1 FROM `{self._project_id}.airtable_replica.contacts` ct "
            "    WHERE ct.account[OFFSET(0)] = a._airtable_record_id "
            "      AND ct.email IS NOT NULL "
            "  )"
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

    def _load_contact_emails(self, *, account_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """Per-account contact emails (lowercase, deduped)."""
        if not account_ids:
            return {}
        sql = (
            "SELECT account[OFFSET(0)] AS account_id, "  # noqa: S608  account_ids are Airtable record ids; no user input
            "  ARRAY_AGG(DISTINCT LOWER(email) IGNORE NULLS) AS emails "
            f"FROM `{self._project_id}.airtable_replica.contacts` "
            f"WHERE account[OFFSET(0)] IN ({in_list(account_ids)}) "
            "  AND email IS NOT NULL "
            "GROUP BY account_id"
        )
        out: dict[str, tuple[str, ...]] = {}
        for r in self._bq.query_rows(sql):
            emails = tuple(e for e in (r.get("emails") or []) if e)
            out[r["account_id"]] = emails
        return out

    def _load_last_triage_inbound(self, *, account_ids: list[str]) -> dict[str, Any]:
        """Last Triage Agent activity per account.

        Mirrors ``EcommerceClientStateLoader._load_last_inbound``: the
        ``tasks.source = 'Triage Agent'`` proxy means the inbound
        triage pipeline classified an email and created an Airtable
        Task; ``tasks._airtable_last_modified`` (= Airtable record
        ``createdTime``) is the inbound timestamp. Same join
        chain we use elsewhere; cheaper than going through
        ``triaged_items.airtable_task_record_id`` since that's NULL
        for ``dedup_skipped`` and "no project match" rows by design
        (ADR 0022, ADR 0026).
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

    def _load_last_approved_or_done_task(self, *, account_ids: list[str]) -> dict[str, Any]:
        """Last owner-actioned task per account.

        ADR 0035 §4 strict filter: ``approval_status = 'Approved' OR
        status = 'Done'``. A pending Triage-drafted task does NOT
        count as the operator engagement. ``last_modified`` captures the
        moment the operator approved / marked done / edited an Approved
        task — that's the engagement moment we want.
        """
        if not account_ids:
            return {}
        sql = (
            "SELECT p.account[OFFSET(0)] AS account_id, "  # noqa: S608  account_ids are Airtable record ids; no user input
            "  MAX(t._airtable_last_modified) AS last_action_at "
            f"FROM `{self._project_id}.airtable_replica.tasks` t "
            f"JOIN `{self._project_id}.airtable_replica.projects` p "
            "  ON t.project[OFFSET(0)] = p._airtable_record_id "
            "WHERE (t.approval_status = 'Approved' OR t.status = 'Done') "
            f"  AND p.account[OFFSET(0)] IN ({in_list(account_ids)}) "
            "GROUP BY account_id"
        )
        out: dict[str, Any] = {}
        for r in self._bq.query_rows(sql):
            out[r["account_id"]] = r["last_action_at"]
        return out

    # ------------------------------------------------ calendar

    def _calendar_recency(
        self,
        *,
        owner_email: str | None,
        attendee_emails: tuple[str, ...],
        since: datetime,
        until: datetime,
    ) -> datetime | None:
        """Calendar source — None on missing dependency or API failure."""
        if self._calendar is None or not owner_email or not attendee_emails:
            return None
        return self._calendar.most_recent_engagement_event(
            owner_email=owner_email,
            attendee_emails=attendee_emails,
            since=since,
            until=until,
        )


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _max_with_source(
    *candidates: tuple[str, datetime | None],
) -> tuple[datetime | None, str | None]:
    """Pick the most recent timestamp + its source label.

    Returns ``(None, None)`` if all candidates are None.
    """
    winner: tuple[str, datetime] | None = None
    for label, ts in candidates:
        if ts is None:
            continue
        if winner is None or ts > winner[1]:
            winner = (label, ts)
    if winner is None:
        return None, None
    return winner[1], winner[0]
