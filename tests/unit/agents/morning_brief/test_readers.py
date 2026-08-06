"""Unit tests for the Morning Brief BQ readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agency_brain.agents.morning_brief.readers import (
    DraftsAwaitingReviewReader,
    OpenTasksForOwnerReader,
    RiskFlagsReader,
    TriagedItemsForOwnerReader,
)


@dataclass
class _FakeBQ:
    rows: list[dict] = field(default_factory=list)
    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return list(self.rows)


def test_triaged_items_reader_filters_and_orders():
    bq = _FakeBQ(
        rows=[
            {
                "item_id": "i-1",
                "severity": "critical",
                "summary": "Escalation",
                "source": "gmail",
                "source_url": "https://x/y",
            },
            {
                "item_id": "i-2",
                "severity": "high",
                "summary": "Deliverable",
                "source": "drive",
                "source_url": None,
            },
        ]
    )
    reader = TriagedItemsForOwnerReader(bq_client=bq, project_id="p", limit=8)
    items = reader.load("owner@example.com")
    assert [i.item_id for i in items] == ["i-1", "i-2"]
    # SQL was rendered with the recipient + limit substituted.
    assert "'owner@example.com'" in bq.last_sql
    assert "LIMIT 8" in bq.last_sql
    assert "actionable = TRUE" in bq.last_sql
    # Only critical/high/medium severities.
    assert "ti.severity IN ('critical', 'high', 'medium')" in bq.last_sql


def test_open_tasks_reader_handles_due_date():
    bq = _FakeBQ(
        rows=[
            {
                "task_id": "t-1",
                "name": "Reply to ClientC",
                "due_date": "2026-05-06",
                "project_name": "Client C Studio",
            },
            {
                "task_id": "t-2",
                "name": "Plan Q3",
                "due_date": None,
                "project_name": None,
            },
        ]
    )
    reader = OpenTasksForOwnerReader(bq_client=bq, project_id="p")
    tasks = reader.load("owner@example.com")
    assert tasks[0].due_date == date(2026, 5, 6)
    assert tasks[1].due_date is None
    assert tasks[1].project_name is None
    # Status filter tolerates NULL via COALESCE; recipient filter compares
    # tasks.owner (singleCollaborator → synced as *email*, no `_extract`
    # annotation) directly against the recipient. Joining team.user (a
    # usrXXX id) against it never matches — the documented gotcha.
    assert "COALESCE(t.status, '') != 'Done'" in bq.last_sql
    assert "LOWER(t.owner) = LOWER('owner@example.com')" in bq.last_sql
    assert "tm.user" not in bq.last_sql


def test_risk_flags_reader_owner_filter():
    bq = _FakeBQ(rows=[])
    reader = RiskFlagsReader(bq_client=bq, project_id="p")
    flags = reader.load("a@b.com")
    assert flags == []
    assert "ao.owner_email = 'a@b.com'" in bq.last_sql
    assert "ao.hipaa = FALSE" in bq.last_sql


def test_drafts_awaiting_reader_global_query():
    bq = _FakeBQ(
        rows=[
            {
                "task_id": "t-7",
                "name": "Q3 lead-gen results",
                "project_name": "Triage Inbox",
                "drafted_at": None,
            }
        ]
    )
    reader = DraftsAwaitingReviewReader(bq_client=bq, project_id="p")
    drafts = reader.load("anyone@nowhere.com")
    assert drafts[0].task_id == "t-7"
    assert "approval_status = 'Drafted by Agent'" in bq.last_sql


def test_quoted_recipient_escapes_single_quote():
    bq = _FakeBQ(rows=[])
    reader = TriagedItemsForOwnerReader(bq_client=bq, project_id="p")
    reader.load("o'malley@example.com")
    # Embedded quote is doubled per SQL escaping convention.
    assert "'o''malley@example.com'" in bq.last_sql
