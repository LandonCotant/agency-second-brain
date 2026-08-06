"""Unit tests for the Brag Spotter BQ readers (ADR 0043 §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from agency_brain.agents.brag_spotter.readers import (
    ExistingWinsForWeekReader,
    RecentDecisionsReader,
    RecentNotesReader,
    RecentReflectionsReader,
    RecentRoutedEventsReader,
    RecentTriagedItemsReader,
)


@dataclass
class _FakeBQ:
    rows: list[dict] = field(default_factory=list)
    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return list(self.rows)


# ---------------------------------------------------------------- triaged items


def test_triaged_items_reader_filters_severity_and_actionable():
    bq = _FakeBQ(
        rows=[
            {
                "item_id": "i-1",
                "triaged_at": datetime(2026, 5, 4, tzinfo=UTC),
                "severity": "critical",
                "reasoning": "Hot lead",
                "source": "gmail",
                "source_url": "https://x/y",
                "positive_goal_achieving": None,
            }
        ]
    )
    reader = RecentTriagedItemsReader(bq_client=bq, project_id="p", lookback_days=7, limit=50)
    items = reader.load()
    assert len(items) == 1
    assert items[0].item_id == "i-1"
    assert items[0].severity == "critical"
    assert "INTERVAL 7 DAY" in bq.last_sql
    assert "actionable = TRUE" in bq.last_sql
    assert "LIMIT 50" in bq.last_sql
    # Tightened 2026-05-14 — narrow to highest-PGA critical items only.
    assert "severity = 'critical'" in bq.last_sql
    assert "positive_goal_achieving = 'strong'" in bq.last_sql


def test_triaged_items_reader_empty_returns_empty_list():
    bq = _FakeBQ(rows=[])
    reader = RecentTriagedItemsReader(bq_client=bq, project_id="p")
    assert reader.load() == []


# ---------------------------------------------------------------- routed events


def test_routed_events_reader_loads_rows():
    bq = _FakeBQ(
        rows=[
            {
                "item_id": "i-1",
                "channel": "google_chat_dm",
                "routed_at": datetime(2026, 5, 4, tzinfo=UTC),
            }
        ]
    )
    reader = RecentRoutedEventsReader(bq_client=bq, project_id="p", lookback_days=7)
    events = reader.load()
    assert len(events) == 1
    assert events[0].channel == "google_chat_dm"
    assert "INTERVAL 7 DAY" in bq.last_sql
    # Tightened 2026-05-14 — dedup by item_id so a single event routed
    # to multiple channels doesn't appear as multiple "wins" to the LLM.
    assert "GROUP BY re.item_id" in bq.last_sql
    assert "MIN(re.routed_at)" in bq.last_sql


# ---------------------------------------------------------------- notes


def test_notes_reader_filters_hipaa_and_failed_extraction():
    bq = _FakeBQ(
        rows=[
            {
                "note_id": "n-1",
                "ingested_at": datetime(2026, 5, 4, tzinfo=UTC),
                "filename": "Voice memo.pdf",
                "extraction_method": "gemini-2.5-flash-audio",
                "markdown_content": "Some content",
                "source_drive_url": "https://drive/x",
            }
        ]
    )
    reader = RecentNotesReader(bq_client=bq, project_id="p")
    notes = reader.load()
    assert len(notes) == 1
    assert "hipaa_isolated = FALSE" in bq.last_sql
    assert "extraction_method != 'failed'" in bq.last_sql
    # Tightened 2026-05-14 — keep user-authored kinds; drop
    # calendar_event/email/decision/win (the last avoids a feedback
    # loop where this week's wins re-feed next week's Brag Spotter).
    assert "note_kind IN ('capture', 'inbox', 'area', 'galaxy')" in bq.last_sql


def test_notes_reader_handles_null_markdown():
    bq = _FakeBQ(
        rows=[
            {
                "note_id": "n-1",
                "ingested_at": datetime(2026, 5, 4, tzinfo=UTC),
                "filename": "f.pdf",
                "extraction_method": "gemini-2.5-flash-pdf",
                "markdown_content": None,
                "source_drive_url": "https://drive/x",
            }
        ]
    )
    reader = RecentNotesReader(bq_client=bq, project_id="p")
    notes = reader.load()
    assert notes[0].markdown_content is None


# ---------------------------------------------------------------- decisions


def test_decisions_reader_skips_drafts():
    """Tightened 2026-05-14 — only confirmed or reviewed_* status counts
    as committed (and thus win-eligible). Drafted/pending = not yet a win."""
    bq = _FakeBQ(rows=[])
    reader = RecentDecisionsReader(bq_client=bq, project_id="p")
    reader.load()
    assert "d.status = 'confirmed'" in bq.last_sql
    assert "d.status LIKE 'reviewed_%'" in bq.last_sql
    # Drafted status must be excluded.
    assert "'drafted'" not in bq.last_sql
    assert "'pending'" not in bq.last_sql


def test_decisions_reader_loads_status():
    bq = _FakeBQ(
        rows=[
            {
                "decision_id": "d-1",
                "decided_at": datetime(2026, 5, 4, tzinfo=UTC),
                "title": "Pursue X",
                "context": "because Y",
                "choice": "X",
                "status": "pending",
            }
        ]
    )
    reader = RecentDecisionsReader(bq_client=bq, project_id="p")
    decisions = reader.load()
    assert decisions[0].status == "pending"


# ---------------------------------------------------------------- reflections


def test_reflections_reader_filters_reflect_mode_only():
    bq = _FakeBQ(rows=[])
    reader = RecentReflectionsReader(bq_client=bq, project_id="p")
    reader.load()
    sql = bq.last_sql
    assert "COALESCE(r.mode, 'reflect') = 'reflect'" in sql
    assert "COALESCE(r.dedup_skipped, FALSE) = FALSE" in sql
    assert "r.success = TRUE" in sql


def test_reflections_reader_coerces_local_date():
    bq = _FakeBQ(
        rows=[
            {
                "reflection_id": "r-1",
                "generated_at": datetime(2026, 5, 4, tzinfo=UTC),
                "local_date": "2026-05-04",
                "body_markdown": "x",
                "sections_used": ["morning_brief"],
            }
        ]
    )
    reader = RecentReflectionsReader(bq_client=bq, project_id="p")
    reflections = reader.load()
    assert reflections[0].local_date == date(2026, 5, 4)
    assert reflections[0].sections_used == ("morning_brief",)


# ---------------------------------------------------------------- existing wins


def test_existing_wins_reader_filters_by_week():
    bq = _FakeBQ(
        rows=[
            {
                "win_id": "w-1",
                "title": "Closed thing",
                "source_kind": "reflection",
                "source_id": "r-1",
            }
        ]
    )
    reader = ExistingWinsForWeekReader(bq_client=bq, project_id="p")
    week = date(2026, 5, 4)
    wins = reader.load(week)
    assert len(wins) == 1
    # Week-of literal is interpolated into the SQL.
    assert "DATE('2026-05-04')" in bq.last_sql
