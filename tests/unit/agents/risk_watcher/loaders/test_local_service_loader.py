"""Unit tests for ``LocalServiceClientStateLoader`` (ADR 0034 + 0035)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.loaders import (
    LocalServiceClientStateLoader,
)
from agency_brain.agents.risk_watcher.models import Segment


@dataclass
class _FakeBQ:
    """Returns canned responses keyed by SQL substring matches."""

    canned: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    captured: list[str] = field(default_factory=list)

    def query_rows(self, sql: str) -> list[dict]:
        self.captured.append(sql)
        for substring, rows in self.canned.items():
            if substring in sql:
                return rows
        return []


@dataclass
class _FakeCalendar:
    """Stub for the ADR 0035 ``CalendarRecencyClient`` Protocol."""

    response_by_subject: dict[str, datetime | None] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_exc: Exception | None = None

    def most_recent_engagement_event(
        self,
        *,
        owner_email: str,
        attendee_emails: tuple[str, ...],
        since: datetime,
        until: datetime,
    ) -> datetime | None:
        self.calls.append(
            {
                "owner_email": owner_email,
                "attendee_emails": attendee_emails,
                "since": since,
                "until": until,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response_by_subject.get(owner_email)


def _now() -> datetime:
    return datetime(2026, 5, 11, 12, tzinfo=UTC)


def _ls_account_row(
    *,
    account_id: str = "recAcctLS1",
    company_name: str = "Client A",
    account_manager: str | None = "owner@example.com",
    primary_project_id: str | None = "recProjLS1",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "company_name": company_name,
        "account_manager": account_manager,
        "primary_project_id": primary_project_id,
    }


# ---------------------------------------------------------------------------
# Loader baseline
# ---------------------------------------------------------------------------


def test_loader_returns_empty_when_no_local_service_accounts() -> None:
    bq = _FakeBQ(canned={"FROM `prj.airtable_replica.accounts` a": []})
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", now_fn=_now)
    assert loader.load() == ()


def test_accounts_query_includes_all_three_filters_and_two_gates() -> None:
    """ADR 0034 segment/status/HIPAA filters + ADR 0035 contract/contact gates."""
    bq = _FakeBQ(canned={"FROM `prj.airtable_replica.accounts` a": []})
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", now_fn=_now)
    loader.load()
    sql = bq.captured[0]
    # ADR 0034 filters
    assert "segment = 'Local Service'" in sql
    assert "status IN ('Active', 'Mature')" in sql
    assert "hipaa = FALSE" in sql
    # ADR 0035 §2 gates
    assert "FROM `prj.airtable_replica.contracts` c" in sql
    assert "c.contract_status = 'Active'" in sql
    assert "c.expiry_date IS NULL OR c.expiry_date >= CURRENT_DATE()" in sql
    assert "FROM `prj.airtable_replica.contacts` ct" in sql
    assert "ct.email IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# Multi-source engagement aggregation (ADR 0035 §1)
# ---------------------------------------------------------------------------


def test_engagement_max_picks_calendar_when_most_recent() -> None:
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
            "WHERE t.source = 'Triage Agent'": [
                {
                    "account_id": "recAcctLS1",
                    "last_inbound_at": datetime(2026, 4, 20, 9, tzinfo=UTC),
                }
            ],
            "(t.approval_status = 'Approved' OR t.status = 'Done')": [
                {
                    "account_id": "recAcctLS1",
                    "last_action_at": datetime(2026, 4, 25, 9, tzinfo=UTC),
                }
            ],
        }
    )
    calendar = _FakeCalendar(
        response_by_subject={
            "owner@example.com": datetime(2026, 5, 4, 12, tzinfo=UTC),
        }
    )
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=calendar, now_fn=_now)

    states = loader.load()

    assert len(states) == 1
    extras = states[0].extras
    assert extras["last_engagement_at"] == datetime(2026, 5, 4, 12, tzinfo=UTC)
    assert extras["winning_source"] == "calendar"
    assert extras["engagement_lookback_days"] == 30
    # Calendar got the contact email list.
    assert calendar.calls[0]["attendee_emails"] == ("client@clientapi.com",)


def test_engagement_max_picks_triage_inbound_when_most_recent() -> None:
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
            "WHERE t.source = 'Triage Agent'": [
                {
                    "account_id": "recAcctLS1",
                    "last_inbound_at": datetime(2026, 5, 8, 9, tzinfo=UTC),
                }
            ],
            "(t.approval_status = 'Approved' OR t.status = 'Done')": [
                {
                    "account_id": "recAcctLS1",
                    "last_action_at": datetime(2026, 4, 25, 9, tzinfo=UTC),
                }
            ],
        }
    )
    calendar = _FakeCalendar(
        response_by_subject={
            "owner@example.com": datetime(2026, 5, 4, 12, tzinfo=UTC),
        }
    )
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=calendar, now_fn=_now)

    extras = loader.load()[0].extras
    assert extras["last_engagement_at"] == datetime(2026, 5, 8, 9, tzinfo=UTC)
    assert extras["winning_source"] == "triage inbound"


def test_engagement_max_picks_task_action_when_most_recent() -> None:
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
            "WHERE t.source = 'Triage Agent'": [],
            "(t.approval_status = 'Approved' OR t.status = 'Done')": [
                {
                    "account_id": "recAcctLS1",
                    "last_action_at": datetime(2026, 5, 9, 9, tzinfo=UTC),
                }
            ],
        }
    )
    calendar = _FakeCalendar(response_by_subject={})
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=calendar, now_fn=_now)

    extras = loader.load()[0].extras
    assert extras["last_engagement_at"] == datetime(2026, 5, 9, 9, tzinfo=UTC)
    assert extras["winning_source"] == "task approval/completion"


def test_all_sources_quiet_returns_none_with_lookback_recorded() -> None:
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
            # All three engagement queries return empty.
        }
    )
    calendar = _FakeCalendar(response_by_subject={"owner@example.com": None})
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=calendar, now_fn=_now)

    extras = loader.load()[0].extras
    assert extras["last_engagement_at"] is None
    assert extras["winning_source"] is None
    assert extras["engagement_lookback_days"] == 30


# ---------------------------------------------------------------------------
# Strict task filter (ADR 0035 §4)
# ---------------------------------------------------------------------------


def test_task_query_uses_strict_approval_status_or_done_filter() -> None:
    bq = _FakeBQ(canned={"FROM `prj.airtable_replica.accounts` a": []})
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", now_fn=_now)
    loader.load()
    # No accounts → only the accounts query runs. Ensure load with one
    # account triggers the task query that contains the filter.
    bq2 = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [],
            "WHERE t.source = 'Triage Agent'": [],
            "(t.approval_status = 'Approved' OR t.status = 'Done')": [],
        }
    )
    loader2 = LocalServiceClientStateLoader(bq=bq2, project_id="prj", calendar=None, now_fn=_now)
    loader2.load()
    # Find the task-action query among captured SQL.
    task_action_sql = next(
        (s for s in bq2.captured if "(t.approval_status = 'Approved' OR t.status = 'Done')" in s),
        None,
    )
    assert task_action_sql is not None
    assert "MAX(t._airtable_last_modified) AS last_action_at" in task_action_sql


# ---------------------------------------------------------------------------
# Calendar dependency edge cases
# ---------------------------------------------------------------------------


def test_no_calendar_client_uses_only_bq_sources() -> None:
    """Test/constrained envs pass calendar=None — engagement still works."""
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
            "WHERE t.source = 'Triage Agent'": [
                {
                    "account_id": "recAcctLS1",
                    "last_inbound_at": datetime(2026, 5, 5, 9, tzinfo=UTC),
                }
            ],
        }
    )
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=None, now_fn=_now)

    extras = loader.load()[0].extras
    assert extras["last_engagement_at"] == datetime(2026, 5, 5, 9, tzinfo=UTC)
    assert extras["winning_source"] == "triage inbound"


def test_account_manager_email_used_directly_without_team_join() -> None:
    """Pinned regression: PR #82 dropped the team JOIN."""

    @dataclass
    class _AssertNoTeamBQ:
        canned: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

        def query_rows(self, sql: str) -> list[dict]:
            assert (
                "airtable_replica.team" not in sql
            ), "Loader must not query team — account_manager is already an email."
            for substring, rows in self.canned.items():
                if substring in sql:
                    return rows
            return []

    bq = _AssertNoTeamBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
        }
    )
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=None, now_fn=_now)

    extras = loader.load()[0].extras
    assert extras["owner_email"] == "owner@example.com"


# ---------------------------------------------------------------------------
# State assembly basics
# ---------------------------------------------------------------------------


def test_state_carries_pending_drafted_tasks_for_approval_slowdown() -> None:
    """The Approval Slowdown signal still depends on pending drafts."""
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [_ls_account_row()],
            "approval_status = 'Drafted by Agent'": [
                {
                    "record_id": "recT1",
                    "task_name": "Follow up on intake form",
                    "created": datetime(2026, 5, 1, 9, tzinfo=UTC),
                    "account_id": "recAcctLS1",
                }
            ],
            "FROM `prj.airtable_replica.contacts`": [
                {"account_id": "recAcctLS1", "emails": ["client@clientapi.com"]}
            ],
        }
    )
    loader = LocalServiceClientStateLoader(bq=bq, project_id="prj", calendar=None, now_fn=_now)

    state = loader.load()[0]
    assert state.account_id == "recAcctLS1"
    assert state.account_name == "Client A"
    assert state.segment == Segment.LOCAL_SERVICE
    assert len(state.extras["pending_drafted_tasks"]) == 1
    assert state.extras["contact_emails"] == ("client@clientapi.com",)
