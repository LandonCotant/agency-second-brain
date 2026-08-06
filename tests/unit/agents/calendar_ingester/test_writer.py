"""Tests for ``CalendarWriter`` — MERGE shape + insert/update classification."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.calendar_ingester.models import (
    EventMetadata,
    NoteRow,
)
from agency_brain.agents.calendar_ingester.writer import CalendarWriter


class _FakeBQ:
    def __init__(self, *, existing: dict[str, str] | None = None) -> None:
        # External_id → embedding_content_hash for "existing" rows.
        self._existing = dict(existing or {})
        self.queries: list[tuple[str, list[dict] | None]] = []

    def query_rows(self, sql: str, parameters=None):
        self.queries.append((sql, parameters))
        if "FROM `" in sql and "external_id IN UNNEST(" in sql:
            ids = (parameters or [{}])[0].get("value") or []
            return [
                {"external_id": eid, "embedding_content_hash": self._existing[eid]}
                for eid in ids
                if eid in self._existing
            ]
        # MERGE — return empty.
        return []


def _row(*, external_id: str, hash_value: str = "abc") -> NoteRow:
    return NoteRow(
        note_id=f"cal-{external_id[:12]}",
        filename=f"[2026-05-09] {external_id}",
        markdown_content="# title\n\nbody",
        source_drive_url="https://calendar/x",
        scope="agency",
        note_kind="calendar_event",
        hipaa_isolated=False,
        external_id=external_id,
        revision_id="2026-05-09T12:00:00+00:00",
        source_drive_file_id=f"cal:primary:{external_id}",
        created_at=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
        embedding=tuple([0.1] * 768),
        embedding_model="text-embedding-005",
        embedding_content_hash=hash_value,
        event_metadata=EventMetadata(
            start_iso="2026-05-09T10:00:00+00:00",
            end_iso="2026-05-09T11:00:00+00:00",
            attendees=("a@b.com",),
            organizer="o@x.com",
            location="Zoom",
            status="confirmed",
        ),
        ingested_at=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )


def test_merge_classifies_inserts_updates_unchanged() -> None:
    bq = _FakeBQ(
        existing={
            "evA": "old_hash",  # different from new → update
            "evB": "abc",  # same → unchanged
        }
    )
    writer = CalendarWriter(bq_query=bq, project_id="p")
    rows = [
        _row(external_id="evA", hash_value="abc"),  # update
        _row(external_id="evB", hash_value="abc"),  # unchanged
        _row(external_id="evC", hash_value="abc"),  # insert
    ]
    result = writer.merge(rows)
    assert result.inserted == 1
    assert result.updated == 1
    assert result.unchanged == 1
    assert result.errors == 0


def test_merge_empty_rows_is_noop() -> None:
    bq = _FakeBQ()
    writer = CalendarWriter(bq_query=bq, project_id="p")
    result = writer.merge([])
    assert result.total_events == 0
    assert bq.queries == []


def test_merge_sql_contains_load_bearing_clauses() -> None:
    """The MERGE must:
    - Match on external_id AND note_kind = 'calendar_event' (so we
      don't accidentally clobber a Drive note that happened to share
      an id collision).
    - Update only when the embedding_content_hash changed (idempotency).
    - INSERT note_kind = 'calendar_event'.
    """
    bq = _FakeBQ()
    writer = CalendarWriter(bq_query=bq, project_id="p")
    writer.merge([_row(external_id="evX", hash_value="abc")])
    # Two queries: SELECT (existing lookup) + MERGE.
    assert len(bq.queries) == 2
    merge_sql = bq.queries[1][0]
    assert "MERGE" in merge_sql
    assert "T.note_kind = 'calendar_event'" in merge_sql
    assert "T.embedding_content_hash != S.embedding_content_hash" in merge_sql
    assert "WHEN NOT MATCHED THEN" in merge_sql


def test_merge_chunks_large_row_sets_under_param_cap() -> None:
    """Rows are chunked at MERGE_BATCH_SIZE so the @rows_json param stays
    under BQ's ~1 MB scalar limit. 120 rows → 1 existing-lookup + 3 MERGEs."""
    from agency_brain.agents.calendar_ingester.writer import MERGE_BATCH_SIZE

    bq = _FakeBQ()
    writer = CalendarWriter(bq_query=bq, project_id="p")
    rows = [_row(external_id=f"ev{i}", hash_value="abc") for i in range(120)]
    result = writer.merge(rows)
    assert result.inserted == 120
    merge_calls = [q for q in bq.queries if "external_id IN UNNEST(" not in q[0]]
    assert len(merge_calls) == 3  # ceil(120 / 50)
    # No single MERGE carries more than the batch size of rows.
    import json as _json

    for sql, params in merge_calls:
        payload = _json.loads(params[0]["value"])
        assert len(payload) <= MERGE_BATCH_SIZE


def test_merge_handles_failure() -> None:
    class _Boom:
        def __init__(self):
            self.calls = 0

        def query_rows(self, sql, parameters=None):
            self.calls += 1
            if self.calls == 1:
                # existing-hash lookup
                return []
            raise RuntimeError("BQ down")

    writer = CalendarWriter(bq_query=_Boom(), project_id="p")
    result = writer.merge([_row(external_id="evX", hash_value="abc")])
    assert result.errors == 1
