"""Unit tests for the notes ingestor's BQ writers + watermark store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from agency_brain.agents.notes_ingestor.models import (
    ExtractionMethod,
    NoteRow,
)
from agency_brain.agents.notes_ingestor.writer import (
    NotesWriteError,
    NotesWriter,
    WatermarkStore,
)


@dataclass
class _FakeRows:
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)
    errors: list = field(default_factory=list)

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.captured.append((table_ref, rows))
        return list(self.errors)


@dataclass
class _FakeQuery:
    """Captures SQL + params; returns canned rows in FIFO order."""

    canned: list[list[dict]] = field(default_factory=list)
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.captured.append((sql, list(parameters or [])))
        if self.canned:
            return self.canned.pop(0)
        return []


@dataclass
class _FakeUpdate:
    """Captures SQL + params; returns ``rows_affected`` per call."""

    rows_affected: int = 1
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)
    raises: Exception | None = None

    def update_rows(self, sql: str, parameters: list[dict] | None = None) -> int:
        if self.raises is not None:
            raise self.raises
        self.captured.append((sql, list(parameters or [])))
        return self.rows_affected


def _row(**overrides) -> NoteRow:
    base = dict(
        note_id="file-1",
        revision_id="rev-1",
        ingested_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 5, 2, 11, 55, tzinfo=UTC),
        source_drive_file_id="file-1",
        source_drive_url="https://drive.google.com/file/d/file-1/view",
        filename="meeting.pdf",
        markdown_content="# meeting\n- note",
        extraction_method=ExtractionMethod.GEMINI_FLASH,
        extraction_confidence=0.95,
        page_count=2,
        hipaa_isolated=False,
    )
    base.update(overrides)
    return NoteRow(**base)


# ---------------------------------------------------------------------------
# NotesWriter
# ---------------------------------------------------------------------------


def test_find_existing_returns_none_without_query_client():
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=None, project_id="p")
    assert writer.find_existing(source_drive_file_id="x", revision_id="y") is None


def test_find_existing_returns_hit_when_row_exists():
    rows = _FakeRows()
    query = _FakeQuery(
        canned=[
            [
                {
                    "note_id": "file-1",
                    "ingested_at": "2026-05-02T11:50:00+00:00",
                }
            ]
        ]
    )
    writer = NotesWriter(bq_rows=rows, bq_query=query, project_id="p")

    hit = writer.find_existing(source_drive_file_id="file-1", revision_id="rev-1")

    assert hit is not None
    assert hit.note_id == "file-1"
    assert hit.ingested_at.tzinfo is not None
    # And the SQL was parameterized correctly.
    captured_sql, captured_params = query.captured[0]
    assert "agent_outputs.notes" in captured_sql
    names = {p["name"] for p in captured_params}
    assert names == {"file_id", "revision_id"}


def test_find_existing_returns_none_when_no_rows():
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])
    writer = NotesWriter(bq_rows=rows, bq_query=query, project_id="p")

    assert writer.find_existing(source_drive_file_id="file-1", revision_id="rev-1") is None


def test_write_inserts_one_row_with_correct_shape():
    rows = _FakeRows()
    writer = NotesWriter(bq_rows=rows, bq_query=None, project_id="p")

    writer.write(_row())

    assert len(rows.captured) == 1
    table_ref, payload = rows.captured[0]
    assert table_ref == "p.agent_outputs.notes"
    assert len(payload) == 1
    row = payload[0]
    assert row["note_id"] == "file-1"
    assert row["revision_id"] == "rev-1"
    assert row["filename"] == "meeting.pdf"
    assert row["extraction_method"] == "gemini-2.5-flash"
    assert row["hipaa_isolated"] is False
    assert row["page_count"] == 2
    # ISO 8601 timestamps must serialize cleanly for streaming inserts.
    assert row["ingested_at"].endswith("+00:00")


def test_write_serializes_hipaa_isolated_true_for_hipaa_folder():
    rows = _FakeRows()
    writer = NotesWriter(bq_rows=rows, bq_query=None, project_id="p")

    writer.write(_row(hipaa_isolated=True))

    assert rows.captured[0][1][0]["hipaa_isolated"] is True


def test_write_raises_when_bq_rejects():
    rows = _FakeRows(errors=[{"errors": [{"reason": "invalid"}]}])
    writer = NotesWriter(bq_rows=rows, bq_query=None, project_id="p")

    with pytest.raises(NotesWriteError):
        writer.write(_row())


# ---------------------------------------------------------------------------
# Embeddings backfill (ADR 0039 §4)
# ---------------------------------------------------------------------------


def test_find_unembedded_rows_returns_empty_without_query_client():
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=None, project_id="p")
    assert writer.find_unembedded_rows(limit=100) == []


def test_find_unembedded_rows_filters_null_or_empty_embedding():
    canned = [
        [
            {"note_id": "n1", "revision_id": "r1", "markdown_content": "x"},
            {"note_id": "n2", "revision_id": "r2", "markdown_content": ""},
        ]
    ]
    query = _FakeQuery(canned=canned)
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=query, project_id="p")

    rows = writer.find_unembedded_rows(limit=50)
    assert len(rows) == 2
    sql, params = query.captured[0]
    assert "embedding IS NULL OR ARRAY_LENGTH(embedding) = 0" in sql
    assert "ORDER BY ingested_at ASC" in sql
    assert params == [{"name": "limit", "type": "INT64", "value": 50}]


def test_update_embedding_issues_parameterized_update():
    update = _FakeUpdate(rows_affected=1)
    writer = NotesWriter(
        bq_rows=_FakeRows(),
        bq_query=_FakeQuery(),
        bq_update=update,
        project_id="p",
    )
    when = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    n = writer.update_embedding(
        note_id="n1",
        revision_id="r1",
        embedding=[0.1, 0.2, 0.3],
        model="text-embedding-005",
        content_hash="abc",
        generated_at=when,
    )
    assert n == 1
    sql, params = update.captured[0]
    assert "UPDATE" in sql
    assert "embedding = @embedding" in sql
    assert "WHERE note_id = @note_id AND revision_id = @revision_id" in sql

    by_name = {p["name"]: p for p in params}
    assert by_name["embedding"]["mode"] == "REPEATED"
    assert by_name["embedding"]["type"] == "FLOAT64"
    assert by_name["embedding"]["value"] == [0.1, 0.2, 0.3]
    assert by_name["model"]["value"] == "text-embedding-005"
    assert by_name["content_hash"]["value"] == "abc"
    assert by_name["note_id"]["value"] == "n1"
    assert by_name["revision_id"]["value"] == "r1"


def test_update_embedding_raises_without_update_client():
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=_FakeQuery(), project_id="p")
    with pytest.raises(NotesWriteError):
        writer.update_embedding(
            note_id="n1",
            revision_id="r1",
            embedding=[],
            model="m",
            content_hash="h",
            generated_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# WatermarkStore
# ---------------------------------------------------------------------------


def test_watermark_read_returns_none_when_no_row():
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])
    store = WatermarkStore(bq_rows=rows, bq_query=query, project_id="p")

    assert store.read("folder-id-1") is None


def test_watermark_read_parses_iso_string():
    rows = _FakeRows()
    query = _FakeQuery(canned=[[{"last_modified_time_seen": "2026-05-02T11:00:00+00:00"}]])
    store = WatermarkStore(bq_rows=rows, bq_query=query, project_id="p")

    ts = store.read("folder-id-1")

    assert ts == datetime(2026, 5, 2, 11, 0, tzinfo=UTC)


def test_watermark_write_inserts_one_row():
    rows = _FakeRows()
    query = _FakeQuery()
    store = WatermarkStore(bq_rows=rows, bq_query=query, project_id="p")

    when = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    store.write(folder_path="folder-id-1", last_modified_time_seen=when)

    assert len(rows.captured) == 1
    table_ref, payload = rows.captured[0]
    assert table_ref == "p.agent_state.notes_ingestor_watermark"
    row = payload[0]
    assert row["folder_path"] == "folder-id-1"
    assert row["last_modified_time_seen"] == "2026-05-02T12:00:00+00:00"
    assert "updated_at" in row


# ---------------------------------------------------------------------------
# ADR 0070 — note_id-keyed MERGE write (Librarian path)
# ---------------------------------------------------------------------------


def test_find_revision_by_note_id_returns_none_without_query_client():
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=None, project_id="p")
    assert writer.find_revision_by_note_id(note_id="x") is None


def test_find_revision_by_note_id_returns_latest_revision():
    query = _FakeQuery(canned=[[{"revision_id": "rev-9"}]])
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=query, project_id="p")

    rev = writer.find_revision_by_note_id(note_id="file-1")

    assert rev == "rev-9"
    sql, params = query.captured[0]
    assert "agent_outputs.notes" in sql
    assert "WHERE note_id = @note_id" in sql
    assert {p["name"] for p in params} == {"note_id"}


def test_find_revision_by_note_id_returns_none_when_absent():
    query = _FakeQuery(canned=[[]])
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=query, project_id="p")
    assert writer.find_revision_by_note_id(note_id="file-1") is None


def test_merge_by_note_id_requires_query_client():
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=None, project_id="p")
    with pytest.raises(NotesWriteError):
        writer.merge_by_note_id(_row())


def test_merge_by_note_id_issues_merge_keyed_on_note_id():
    import json

    query = _FakeQuery()
    writer = NotesWriter(bq_rows=_FakeRows(), bq_query=query, project_id="p")

    writer.merge_by_note_id(_row(note_kind=None, scope=None, embedding=(0.1, 0.2, 0.3)))

    assert len(query.captured) == 1
    sql, params = query.captured[0]
    assert "MERGE `p.agent_outputs.notes`" in sql
    assert "ON T.note_id = S.note_id" in sql
    # triaged_item_id is intentionally NOT clobbered on UPDATE.
    assert "triaged_item_id" not in sql.split("WHEN NOT MATCHED")[0]
    # The whole row travels as one JSON-array STRING parameter.
    assert [p["name"] for p in params] == ["rows_json"]
    payload = json.loads(params[0]["value"])
    assert len(payload) == 1
    assert payload[0]["note_id"] == "file-1"
    assert payload[0]["revision_id"] == "rev-1"
    assert payload[0]["embedding"] == [0.1, 0.2, 0.3]
    # Unset PKM fields serialize as explicit null so JSON paths resolve.
    assert payload[0]["note_kind"] is None
    assert payload[0]["embedding_generated_at"] is None
