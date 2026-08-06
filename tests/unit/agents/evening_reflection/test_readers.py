"""Unit tests for the Evening Reflection BQ readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest
from agency_brain.agents.evening_reflection.readers import (
    ActiveRiskFlagsTodayReader,
    CompletedTasksTodayReader,
    InFlightDecisionsReader,
    MorningBriefForTodayReader,
    RecentVoiceMemosReader,
    TriagedItemsTodayReader,
)


@dataclass
class _FakeBQ:
    rows: list[dict] = field(default_factory=list)
    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return list(self.rows)


# --------------------------------------------------- completed tasks


def test_completed_tasks_reader_filters_status_done_and_completed_date():
    bq = _FakeBQ(
        rows=[
            {
                "task_id": "t-1",
                "name": "Reply to ClientC",
                "project_name": "Client C Studio",
                "completed_date": "2026-05-05",
            }
        ]
    )
    reader = CompletedTasksTodayReader(bq_client=bq, project_id="p", limit=20)
    items = reader.load("owner@example.com", date(2026, 5, 5))
    assert items[0].task_id == "t-1"
    assert items[0].project_name == "Client C Studio"
    assert items[0].completed_date == date(2026, 5, 5)
    # SQL renders status filter, completed_date param, recipient join.
    assert "t.status = 'Done'" in bq.last_sql
    assert "t.completed_date = DATE '2026-05-05'" in bq.last_sql
    # tasks.owner syncs as the collaborator *email* (no `_extract`
    # annotation) — compare directly; team.user is a usrXXX id and never
    # matches it.
    assert "LOWER(t.owner) = LOWER('owner@example.com')" in bq.last_sql
    assert "tm.user" not in bq.last_sql
    assert "LIMIT 20" in bq.last_sql


def test_completed_tasks_reader_handles_empty_result():
    bq = _FakeBQ(rows=[])
    reader = CompletedTasksTodayReader(bq_client=bq, project_id="p")
    assert reader.load("a@b.com", date(2026, 5, 5)) == []


def test_completed_tasks_reader_coerces_missing_completed_date():
    bq = _FakeBQ(
        rows=[
            {
                "task_id": "t-2",
                "name": "Quick errand",
                "project_name": None,
                "completed_date": None,
            }
        ]
    )
    reader = CompletedTasksTodayReader(bq_client=bq, project_id="p")
    items = reader.load("a@b.com", date(2026, 5, 5))
    assert items[0].completed_date is None


# --------------------------------------------------- triaged today


def test_triaged_today_reader_includes_all_severities():
    bq = _FakeBQ(
        rows=[
            {
                "item_id": "i-1",
                "severity": "critical",
                "summary": "Esc",
                "source": "gmail",
                "action_type": "do_now",
                "source_url": "https://x/y",
            },
            {
                "item_id": "i-2",
                "severity": "info",
                "summary": "FYI",
                "source": "airtable",
                "action_type": "wait",
                "source_url": None,
            },
        ]
    )
    reader = TriagedItemsTodayReader(bq_client=bq, project_id="p")
    items = reader.load("owner@example.com", date(2026, 5, 5))
    assert [i.severity for i in items] == ["critical", "info"]
    assert "WHEN 'info' THEN 4" in bq.last_sql  # all severities ranked


def test_triaged_today_reader_keys_on_local_date_and_tz():
    bq = _FakeBQ(rows=[])
    reader = TriagedItemsTodayReader(bq_client=bq, project_id="p", timezone="America/Los_Angeles")
    reader.load("a@b.com", date(2026, 5, 5))
    # SQL must convert triaged_at into the requested TZ before DATE().
    assert "DATE(ti.triaged_at, 'America/Los_Angeles') = DATE '2026-05-05'" in bq.last_sql
    assert "ti.owner_email = 'a@b.com'" in bq.last_sql


def test_triaged_today_reader_does_not_filter_to_actionable():
    """Reflection wants everything that came through, not just actionable."""
    bq = _FakeBQ(rows=[])
    reader = TriagedItemsTodayReader(bq_client=bq, project_id="p")
    reader.load("a@b.com", date(2026, 5, 5))
    assert "actionable" not in bq.last_sql


def test_triaged_today_reader_rejects_invalid_timezone():
    """tz is interpolated raw into SQL; a bad REFLECTION_TIMEZONE must fail
    loud at query-build, not silently empty the section or inject SQL."""
    from zoneinfo import ZoneInfoNotFoundError

    bq = _FakeBQ(rows=[])
    reader = TriagedItemsTodayReader(
        bq_client=bq, project_id="p", timezone="Pacific'); DROP TABLE x--"
    )
    with pytest.raises(ZoneInfoNotFoundError):
        reader.load("a@b.com", date(2026, 5, 5))
    assert not bq.last_sql  # raised at query-build; query never issued


# --------------------------------------------------- morning brief


def test_morning_brief_reader_returns_snippet_when_row_present():
    bq = _FakeBQ(
        rows=[
            {
                "brief_id": "b-1",
                "body_markdown": "# Top 3\n- ...",
                "sections_used": ["top_three", "calendar"],
            }
        ]
    )
    reader = MorningBriefForTodayReader(bq_client=bq, project_id="p")
    brief = reader.load("a@b.com", date(2026, 5, 5))
    assert brief is not None
    assert brief.brief_id == "b-1"
    assert brief.sections_used == ("top_three", "calendar")
    # Pulls the most recent successful brief with a real body.
    assert "success = TRUE" in bq.last_sql
    assert "ORDER BY generated_at DESC" in bq.last_sql
    assert "LIMIT 1" in bq.last_sql


def test_morning_brief_reader_returns_none_when_no_row():
    bq = _FakeBQ(rows=[])
    reader = MorningBriefForTodayReader(bq_client=bq, project_id="p")
    assert reader.load("a@b.com", date(2026, 5, 5)) is None


def test_morning_brief_reader_handles_null_sections():
    bq = _FakeBQ(
        rows=[
            {
                "brief_id": "b-1",
                "body_markdown": "Quiet day",
                "sections_used": None,  # NULL repeated → empty tuple
            }
        ]
    )
    reader = MorningBriefForTodayReader(bq_client=bq, project_id="p")
    brief = reader.load("a@b.com", date(2026, 5, 5))
    assert brief is not None
    assert brief.sections_used == ()


# --------------------------------------------------- active risk flags


def test_active_risk_flags_reader_filters_resolved_at_null():
    bq = _FakeBQ(rows=[])
    reader = ActiveRiskFlagsTodayReader(bq_client=bq, project_id="p")
    reader.load("a@b.com", date(2026, 5, 5))
    assert "rf.resolved_at IS NULL" in bq.last_sql
    assert "ao.hipaa = FALSE" in bq.last_sql
    assert "ao.owner_email = 'a@b.com'" in bq.last_sql


def test_active_risk_flags_reader_keys_on_local_date_and_tz():
    bq = _FakeBQ(rows=[])
    reader = ActiveRiskFlagsTodayReader(
        bq_client=bq, project_id="p", timezone="America/Los_Angeles"
    )
    reader.load("a@b.com", date(2026, 5, 5))
    assert "DATE(rf.flagged_at, 'America/Los_Angeles') = DATE '2026-05-05'" in bq.last_sql


def test_active_risk_flags_reader_carries_account_and_reasoning():
    bq = _FakeBQ(
        rows=[
            {
                "flag_id": "f-1",
                "severity": "critical",
                "pattern_name": "OwnerDisengagement",
                "reasoning": "No engagement in 14 days.",
                "account_name": "Client A",
            }
        ]
    )
    reader = ActiveRiskFlagsTodayReader(bq_client=bq, project_id="p")
    flags = reader.load("a@b.com", date(2026, 5, 5))
    assert flags[0].flag_id == "f-1"
    assert flags[0].account_name == "Client A"
    assert flags[0].reasoning == "No engagement in 14 days."


# --------------------------------------------------- escape behavior


def test_recipient_email_with_quote_is_doubled():
    bq = _FakeBQ(rows=[])
    reader = CompletedTasksTodayReader(bq_client=bq, project_id="p")
    reader.load("o'malley@example.com", date(2026, 5, 5))
    # Embedded quote escaped per SQL convention.
    assert "'o''malley@example.com'" in bq.last_sql


# --------------------------------------------------- recent voice memos (ADR 0040)


def test_recent_voice_memos_reader_filters_audio_extraction_and_24h_window():
    bq = _FakeBQ(rows=[])
    reader = RecentVoiceMemosReader(bq_client=bq, project_id="p", limit=15)
    reader.load()
    # ADR 0040 §3 — extraction_method is the discriminator.
    assert "extraction_method = 'gemini-2.5-flash-audio'" in bq.last_sql
    # ADR 0040 §2 — 24h rolling window, NOT DATE(...) = local_date.
    assert "INTERVAL 24 HOUR" in bq.last_sql
    assert "TIMESTAMP_SUB(CURRENT_TIMESTAMP()" in bq.last_sql
    # ADR 0040 §3 — HIPAA filter required.
    assert "hipaa_isolated = FALSE" in bq.last_sql
    assert "LIMIT 15" in bq.last_sql


def test_recent_voice_memos_reader_parses_rows():
    bq = _FakeBQ(
        rows=[
            {
                "note_id": "captures-recABC",
                "markdown_content": "I should follow up with Client A tomorrow.",
                "ingested_at": datetime(2026, 5, 5, 20, 30, tzinfo=UTC),
            },
            {
                "note_id": "captures-recDEF",
                "markdown_content": "Idea: rebrand the lead-gen page.",
                "ingested_at": "2026-05-05T19:00:00+00:00",
            },
        ]
    )
    reader = RecentVoiceMemosReader(bq_client=bq, project_id="p")
    memos = reader.load()
    assert [m.note_id for m in memos] == ["captures-recABC", "captures-recDEF"]
    # ISO-string timestamp coerced to UTC datetime.
    assert memos[1].ingested_at.tzinfo is UTC
    assert memos[1].ingested_at.hour == 19


def test_recent_voice_memos_reader_handles_empty_result():
    bq = _FakeBQ(rows=[])
    reader = RecentVoiceMemosReader(bq_client=bq, project_id="p")
    assert reader.load() == []


def test_recent_voice_memos_reader_takes_no_recipient_param():
    """Voice memos are personal, not per-account. The reader signature
    must NOT require recipient_email — that contract is what makes it
    safe to skip the reader in PROMPT mode without breaking PROMPT
    callers."""
    bq = _FakeBQ(rows=[])
    reader = RecentVoiceMemosReader(bq_client=bq, project_id="p")
    # Calling without arguments must succeed.
    assert reader.load() == []


# --------------------------------------------------- in-flight decisions (ADR 0040)


def test_in_flight_decisions_reader_filters_status_and_30d_window():
    bq = _FakeBQ(rows=[])
    reader = InFlightDecisionsReader(bq_client=bq, project_id="p", limit=8)
    reader.load()
    # ADR 0040 §4 — draft + pending are the in-flight states.
    assert "status IN ('draft', 'pending')" in bq.last_sql
    # ADR 0040 §4 — 30d window.
    assert "INTERVAL 30 DAY" in bq.last_sql
    assert "TIMESTAMP_SUB(CURRENT_TIMESTAMP()" in bq.last_sql
    assert "LIMIT 8" in bq.last_sql


def test_in_flight_decisions_reader_parses_rows():
    bq = _FakeBQ(
        rows=[
            {
                "decision_id": "dec-1",
                "title": "Renew Client A engagement",
                "context": "Q3 contract expires 2026-06-30",
                "status": "draft",
                "decided_at": datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
            },
            {
                "decision_id": "dec-2",
                "title": "Hire a part-time copy editor",
                "context": None,
                "status": "pending",
                "decided_at": "2026-04-28T10:30:00Z",
            },
        ]
    )
    reader = InFlightDecisionsReader(bq_client=bq, project_id="p")
    decisions = reader.load()
    assert [d.decision_id for d in decisions] == ["dec-1", "dec-2"]
    assert decisions[0].status == "draft"
    assert decisions[1].context is None
    assert decisions[1].decided_at.tzinfo is UTC


def test_in_flight_decisions_reader_handles_empty_result():
    bq = _FakeBQ(rows=[])
    reader = InFlightDecisionsReader(bq_client=bq, project_id="p")
    assert reader.load() == []
