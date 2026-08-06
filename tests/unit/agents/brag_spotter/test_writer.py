"""Unit tests for the Brag Spotter wins writer + dedup (ADR 0043 §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from agency_brain.agents.brag_spotter.models import WinCandidate
from agency_brain.agents.brag_spotter.writer import (
    WinsWriter,
    build_win_row,
    win_id_for,
)


@dataclass
class _FakeBQRows:
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)
    errors_to_return: list = field(default_factory=list)

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.captured.append((table_ref, rows))
        return list(self.errors_to_return)


@dataclass
class _FakeBQQuery:
    rows_to_return: list[dict] = field(default_factory=list)
    last_sql: str = ""
    last_parameters: list[dict] | None = None

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        self.last_parameters = parameters
        return list(self.rows_to_return)


def _candidate(**overrides) -> WinCandidate:
    base = dict(
        title="Closed Acme",
        summary="5-day loop",
        source_kind="decision",
        source_id="d-1",
        evidence_links=("https://x/y",),
    )
    base.update(overrides)
    return WinCandidate(**base)


# ---------------------------------------------------------------- ID format


def test_win_id_for_includes_week_and_hash():
    wid = win_id_for(week_of=date(2026, 5, 4), title="Closed Acme")
    assert wid.startswith("brag_spotter-2026-05-04-")
    assert len(wid.split("-")[-1]) == 12


def test_win_id_for_stable_under_trivial_title_variation():
    a = win_id_for(week_of=date(2026, 5, 4), title="Closed Acme")
    b = win_id_for(week_of=date(2026, 5, 4), title="closed  acme.")
    assert a == b


def test_win_id_for_changes_with_week():
    a = win_id_for(week_of=date(2026, 5, 4), title="Closed Acme")
    b = win_id_for(week_of=date(2026, 5, 11), title="Closed Acme")
    assert a != b


# ---------------------------------------------------------------- row builder


def test_build_win_row_uses_per_source_kind():
    row = build_win_row(
        _candidate(source_kind="triaged_item", source_id="i-1"),
        win_id="brag_spotter-2026-05-04-abc123",
        week_of=date(2026, 5, 4),
        agent_run_id="run-x",
        now=datetime(2026, 5, 4, 18, 0, tzinfo=UTC),
    )
    assert row["source_kind"] == "triaged_item"
    assert row["source_id"] == "i-1"
    assert row["title"] == "Closed Acme"
    assert row["week_of"] == "2026-05-04"
    assert row["evidence_links"] == ["https://x/y"]
    assert row["agent_run_id"] == "run-x"


# ---------------------------------------------------------------- writer


def test_writer_inserts_when_dedup_miss():
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery(rows_to_return=[])
    writer = WinsWriter(bq=bq, bq_query=bq_query, project_id="p")
    outcome = writer.write(
        _candidate(),
        week_of=date(2026, 5, 4),
        agent_run_id="run-x",
    )
    assert outcome.written is True
    assert outcome.error is None
    assert len(bq.captured) == 1
    table_ref, rows = bq.captured[0]
    assert table_ref == "p.agent_outputs.wins"
    assert rows[0]["win_id"] == outcome.win_id
    # Dedup pre-check parameterized on win_id.
    assert bq_query.last_parameters is not None
    assert bq_query.last_parameters[0]["name"] == "value"
    assert bq_query.last_parameters[0]["type"] == "STRING"


def test_writer_skips_on_dedup_hit():
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery(rows_to_return=[{"1": 1}])
    writer = WinsWriter(bq=bq, bq_query=bq_query, project_id="p")
    outcome = writer.write(
        _candidate(),
        week_of=date(2026, 5, 4),
        agent_run_id="run-x",
    )
    assert outcome.written is False
    assert outcome.error is None
    assert bq.captured == []  # no INSERT


def test_writer_returns_error_on_bq_rejection():
    bq = _FakeBQRows(errors_to_return=[{"reason": "invalid"}])
    bq_query = _FakeBQQuery(rows_to_return=[])
    writer = WinsWriter(bq=bq, bq_query=bq_query, project_id="p")
    outcome = writer.write(
        _candidate(),
        week_of=date(2026, 5, 4),
        agent_run_id="run-x",
    )
    assert outcome.written is False
    assert outcome.error is not None
    assert "BQ rejected" in outcome.error


def test_writer_table_ref_default_dataset():
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery()
    writer = WinsWriter(bq=bq, bq_query=bq_query, project_id="proj")
    assert writer.table_ref == "proj.agent_outputs.wins"
