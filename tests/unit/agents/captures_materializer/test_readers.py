"""Unit tests for ``readers.CapturesReader``."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.captures_materializer.models import (
    CaptureKind,
    CaptureScope,
)
from agency_brain.agents.captures_materializer.readers import CapturesReader


class _FakeQuery:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_sql: str | None = None
        self.last_params: list[dict] | None = None

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        self.last_params = parameters
        return self._rows


def test_read_unsynced_filters_synced_false_and_orders() -> None:
    reader = CapturesReader(bq_query=_FakeQuery([]), project_id="p")
    reader.read_unsynced(limit=50)
    q = reader._query  # type: ignore[attr-defined]
    assert "COALESCE(synced, FALSE) = FALSE" in q.last_sql
    assert "ORDER BY captured_at ASC" in q.last_sql
    assert q.last_params == [{"name": "limit", "type": "INT64", "value": 50}]


def test_read_unsynced_returns_captures_for_well_formed_rows() -> None:
    rows = [
        {
            "record_id": "rec1",
            "captured_at": datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
            "note_text": "first",
            "kind": "note",
            "scope_hint": "personal",
        },
        {
            "record_id": "rec2",
            "captured_at": "2026-05-06T10:00:00+00:00",  # ISO string variant
            "note_text": "second",
            "kind": "win",
            "scope_hint": "agency",
        },
    ]
    reader = CapturesReader(bq_query=_FakeQuery(rows), project_id="p")
    out = reader.read_unsynced()
    assert len(out) == 2
    assert out[0].record_id == "rec1"
    assert out[0].kind is CaptureKind.NOTE
    assert out[0].scope is CaptureScope.PERSONAL
    assert out[1].kind is CaptureKind.WIN
    assert out[1].scope is CaptureScope.AGENCY


def test_read_unsynced_skips_rows_missing_required_fields() -> None:
    rows = [
        {"record_id": "", "captured_at": datetime.now(UTC), "note_text": "x", "kind": "note"},
        {"record_id": "r1", "captured_at": datetime.now(UTC), "note_text": "", "kind": "note"},
        {"record_id": "r2", "captured_at": datetime.now(UTC), "note_text": "x", "kind": ""},
        {"record_id": "r3", "captured_at": None, "note_text": "x", "kind": "note"},
        # Valid one to confirm we get something
        {
            "record_id": "rgood",
            "captured_at": datetime.now(UTC),
            "note_text": "good",
            "kind": "todo",
        },
    ]
    reader = CapturesReader(bq_query=_FakeQuery(rows), project_id="p")
    out = reader.read_unsynced()
    assert [c.record_id for c in out] == ["rgood"]


def test_read_unsynced_unknown_kind_filters_row() -> None:
    rows = [
        {
            "record_id": "rec1",
            "captured_at": datetime.now(UTC),
            "note_text": "x",
            "kind": "rogue",
            "scope_hint": "personal",
        },
    ]
    reader = CapturesReader(bq_query=_FakeQuery(rows), project_id="p")
    assert reader.read_unsynced() == []


def test_read_unsynced_unknown_scope_falls_back_to_personal() -> None:
    rows = [
        {
            "record_id": "rec1",
            "captured_at": datetime.now(UTC),
            "note_text": "x",
            "kind": "note",
            "scope_hint": "weird",
        },
    ]
    reader = CapturesReader(bq_query=_FakeQuery(rows), project_id="p")
    out = reader.read_unsynced()
    assert len(out) == 1
    assert out[0].scope is CaptureScope.PERSONAL


def test_read_unsynced_missing_scope_hint_defaults_personal() -> None:
    """Sync writes NULL when the form leaves Scope Hint blank — should not drop the row."""
    rows = [
        {
            "record_id": "rec1",
            "captured_at": datetime.now(UTC),
            "note_text": "x",
            "kind": "note",
            "scope_hint": None,
        },
    ]
    reader = CapturesReader(bq_query=_FakeQuery(rows), project_id="p")
    out = reader.read_unsynced()
    assert len(out) == 1
    assert out[0].scope is CaptureScope.PERSONAL


def test_table_ref_format() -> None:
    reader = CapturesReader(bq_query=_FakeQuery([]), project_id="proj-x")
    assert reader.table_ref == "proj-x.airtable_replica.captures"
