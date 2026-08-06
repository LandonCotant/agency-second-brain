"""Tests for ``LibrarianIngestor`` (Phase G — Librarian-as-ingestor).

ADR 0070 — the write path is a MERGE keyed on ``note_id`` (== Drive
``file_id``), so re-ingesting a changed file UPDATEs its single row
instead of appending a new revision row each sweep. ``_FakeBQ`` below
simulates the table so the idempotency guarantee can be asserted
end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agency_brain.agents.librarian.ingestor import (
    IngestOutcome,
    LibrarianIngestor,
)
from agency_brain.agents.librarian.models import AreaFolder, DropFile
from agency_brain.agents.notes_ingestor.embedder import Embedder
from agency_brain.agents.notes_ingestor.writer import NotesWriter


class _FakeBQ:
    """Simulates ``agent_outputs.notes`` so the MERGE upsert is observable.

    ``query_rows`` dispatches on the SQL: the note_id pre-check SELECT reads
    the simulated table; the MERGE upserts into it keyed on note_id. The row
    count therefore reflects the real append-vs-replace behavior.
    """

    def __init__(self) -> None:
        self.table: dict[str, dict] = {}
        self.merge_calls: list[dict] = []
        self.inserted: list[tuple[str, list[dict]]] = []

    def seed(self, row: dict) -> None:
        self.table[row["note_id"]] = row

    def insert_rows_json(self, table_ref, rows):
        self.inserted.append((table_ref, list(rows)))
        return []

    def query_rows(self, sql, parameters=None):
        params = {p["name"]: p["value"] for p in (parameters or [])}
        if "MERGE" in sql:
            payload = json.loads(params["rows_json"])
            for row in payload:
                self.table[row["note_id"]] = row
                self.merge_calls.append(row)
            return []
        if "SELECT revision_id" in sql:
            note_id = params.get("note_id")
            existing = self.table.get(note_id)
            return [{"revision_id": existing["revision_id"]}] if existing else []
        return []


class _FakeEmbedder(Embedder):
    def __init__(self, *, vector=None, raises=None) -> None:
        self._vector = vector if vector is not None else [0.1] * 768
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def embed(self, *, text: str, model: str) -> list[float]:
        self.calls.append((text, model))
        if self._raises is not None:
            raise self._raises
        return self._vector


class _FakeDriveMeta:
    def __init__(self, *, meta=None, raises=None) -> None:
        self._meta = meta or {
            "id": "f-1",
            "headRevisionId": "rev-1",
            "modifiedTime": "2026-05-08T12:00:00Z",
            "webViewLink": "https://drive/f-1",
        }
        self._raises = raises

    def get_file_meta(self, file_id):
        if self._raises is not None:
            raise self._raises
        return dict(self._meta)


def _drop_file(**overrides):
    base = dict(
        file_id="f-1",
        name="clienta-meeting-notes.md",
        mime_type="text/markdown",
        parent_folder_id="drop-1",
        parent_folder_role="drop",
        modified_time=datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
        web_view_link="https://drive/f-1",
    )
    base.update(overrides)
    return DropFile(**base)


def _make_ingestor(
    *,
    bq=None,
    embedder=None,
    drive_meta=None,
):
    bq = bq or _FakeBQ()
    embedder = embedder or _FakeEmbedder()
    drive_meta = drive_meta or _FakeDriveMeta()
    writer = NotesWriter(bq_rows=bq, bq_query=bq, project_id="p")
    ing = LibrarianIngestor(
        notes_writer=writer,
        embedder=embedder,
        drive_meta=drive_meta,
    )
    return ing, bq, embedder, drive_meta


def test_ingest_writes_a_row_with_embedding() -> None:
    ing, bq, embedder, _ = _make_ingestor()
    dest = AreaFolder(
        id="dest-1",
        name="clienta-pi",
        path="clients/clienta-pi/08_MEETING_NOTES",
        root_id="clients-root",
        root_label="clients",
    )
    outcome = ing.ingest(
        drop_file=_drop_file(),
        dest_folder=dest,
        markdown="Client A meeting on Thursday — strategy refresh.",
    )
    assert isinstance(outcome, IngestOutcome)
    assert outcome.written is True
    assert outcome.embedded is True
    assert outcome.note_id == "f-1"
    assert len(bq.merge_calls) == 1
    assert list(bq.table.keys()) == ["f-1"]
    row = bq.table["f-1"]
    assert row["note_id"] == "f-1"
    assert row["revision_id"] == "rev-1"
    assert row["scope"] == "agency"  # clients root → agency
    assert row["note_kind"] == "area"
    assert row["hipaa_isolated"] is False
    assert row["filename"] == "clienta-meeting-notes.md"
    assert row["embedding"]
    assert "librarian: dest=clients/clienta-pi/08_MEETING_NOTES" in row["extraction_notes"]


def test_ingest_uses_personal_scope_for_brain_root() -> None:
    ing, bq, _, _ = _make_ingestor()
    dest = AreaFolder(
        id="d", name="personal", path="brain/personal", root_id="r", root_label="brain"
    )
    ing.ingest(drop_file=_drop_file(), dest_folder=dest, markdown="hello")
    assert bq.table["f-1"]["scope"] == "personal"


def test_ingest_with_no_dest_folder_marks_uncategorized() -> None:
    ing, bq, _, _ = _make_ingestor()
    outcome = ing.ingest(drop_file=_drop_file(), dest_folder=None, markdown="content")
    assert outcome.written is True
    row = bq.table["f-1"]
    assert "uncategorized" in row["extraction_notes"]
    # Default scope when no root label is known: agency. Unclassified files
    # default to the wider net so business-context agents (Morning Brief,
    # Reflection's Areas RAG) can still surface them.
    assert row["scope"] == "agency"


def test_ingest_skips_empty_content() -> None:
    ing, bq, embedder, _ = _make_ingestor()
    outcome = ing.ingest(
        drop_file=_drop_file(),
        dest_folder=None,
        markdown="   \n\n   ",
    )
    assert outcome.written is False
    assert outcome.note_id is None
    assert bq.merge_calls == []
    assert bq.table == {}
    assert embedder.calls == []


def test_ingest_dedup_hit_skips_embed_and_merge() -> None:
    """A re-ingest with an unchanged revision short-circuits: no embed,
    no MERGE — and the existing row is left untouched (ADR 0070)."""
    ing, bq, embedder, _ = _make_ingestor()
    # Seed the table as if a prior sweep already wrote this note at rev-1
    # (the revision _FakeDriveMeta reports), so the pre-check matches.
    bq.seed({"note_id": "f-1", "revision_id": "rev-1"})
    outcome = ing.ingest(
        drop_file=_drop_file(),
        dest_folder=None,
        markdown="re-process of already ingested file",
    )
    assert outcome.deduped is True
    assert outcome.written is False
    assert outcome.note_id == "f-1"
    assert bq.merge_calls == []  # no write on dedup hit
    assert embedder.calls == []  # and no wasted embed


def test_reingest_changed_content_replaces_single_row() -> None:
    """The core ADR 0070 guarantee: a changed file UPDATEs its one row
    rather than stacking a new revision row each weekly sweep."""
    bq = _FakeBQ()

    ing1, _, _, _ = _make_ingestor(
        bq=bq,
        drive_meta=_FakeDriveMeta(meta={"id": "f-1", "headRevisionId": "rev-1"}),
    )
    ing1.ingest(drop_file=_drop_file(), dest_folder=None, markdown="week one body")

    ing2, _, _, _ = _make_ingestor(
        bq=bq,
        drive_meta=_FakeDriveMeta(meta={"id": "f-1", "headRevisionId": "rev-2"}),
    )
    out2 = ing2.ingest(drop_file=_drop_file(), dest_folder=None, markdown="week two body")

    assert out2.written is True
    assert len(bq.merge_calls) == 2  # both sweeps wrote...
    assert list(bq.table.keys()) == ["f-1"]  # ...but only ONE row survives
    assert bq.table["f-1"]["revision_id"] == "rev-2"
    assert bq.table["f-1"]["markdown_content"] == "week two body"


def test_ingest_continues_when_embedder_fails() -> None:
    embedder = _FakeEmbedder(raises=RuntimeError("embed boom"))
    ing, bq, _, _ = _make_ingestor(embedder=embedder)
    outcome = ing.ingest(
        drop_file=_drop_file(),
        dest_folder=None,
        markdown="content the embedder cannot embed",
    )
    # Row still written; embedded=False so backfill picks it up later.
    assert outcome.written is True
    assert outcome.embedded is False
    assert not bq.table["f-1"]["embedding"]


def test_ingest_meta_failure_returns_error() -> None:
    drive = _FakeDriveMeta(raises=RuntimeError("drive 5xx"))
    ing, bq, _, _ = _make_ingestor(drive_meta=drive)
    outcome = ing.ingest(drop_file=_drop_file(), dest_folder=None, markdown="x")
    assert outcome.written is False
    assert outcome.error and "meta_fetch" in outcome.error
    assert bq.merge_calls == []


def test_ingest_falls_back_to_content_hash_when_no_revision() -> None:
    import hashlib

    drive = _FakeDriveMeta(meta={"id": "f-1", "modifiedTime": "2026-05-08T12:00:00Z"})
    ing, bq, _, _ = _make_ingestor(drive_meta=drive)
    ing.ingest(drop_file=_drop_file(), dest_folder=None, markdown="x")
    row = bq.table["f-1"]
    # No headRevisionId: revision_id keys on a content hash, NOT modifiedTime
    # (which advances on rename/re-share and would defeat dedup).
    expected = "sha256:" + hashlib.sha256(b"x").hexdigest()
    assert row["revision_id"] == expected


def test_ingest_content_hash_is_stable_across_modified_time_changes() -> None:
    """Same content, different modifiedTime → same revision_id, so the
    second tick dedups instead of writing a duplicate corpus row."""
    import hashlib

    expected = "sha256:" + hashlib.sha256(b"same body").hexdigest()
    for mtime in ("2026-05-08T12:00:00Z", "2026-05-09T09:30:00Z"):
        drive = _FakeDriveMeta(meta={"id": "f-1", "modifiedTime": mtime})
        ing, bq, _, _ = _make_ingestor(drive_meta=drive)
        ing.ingest(drop_file=_drop_file(), dest_folder=None, markdown="same body")
        assert bq.table["f-1"]["revision_id"] == expected


# --------------------------------------------------------------- ADR 0054


def test_ingest_bucket_resources_writes_note_kind_resource() -> None:
    """ADR 0054 — destination folder bucket ``resources`` lands the row
    as ``note_kind=resource`` + ``scope=personal`` (Resources lives in
    Brain)."""
    ing, bq, _, _ = _make_ingestor()
    dest = AreaFolder(
        id="r-1",
        name="templates",
        path="resources/templates",
        root_id="res-root",
        root_label="resources",
        bucket="resources",
    )
    ing.ingest(
        drop_file=_drop_file(name="meeting-agenda-template.md"),
        dest_folder=dest,
        markdown="# Meeting Agenda\n\n1. Goals\n2. Discussion\n",
    )
    row = bq.table["f-1"]
    assert row["note_kind"] == "resource"
    assert row["scope"] == "personal"


def test_ingest_bucket_areas_under_clients_label_still_area_agency() -> None:
    """Sanity: a clients-root candidate is bucket=areas (the default),
    so the row stays note_kind=area + scope=agency. Guards against ADR
    0054 changes regressing clients work."""
    ing, bq, _, _ = _make_ingestor()
    dest = AreaFolder(
        id="c-1",
        name="clienta-pi",
        path="clients/clienta-pi",
        root_id="clients-root",
        root_label="clients",
        bucket="areas",
    )
    ing.ingest(drop_file=_drop_file(), dest_folder=dest, markdown="content")
    row = bq.table["f-1"]
    assert row["note_kind"] == "area"
    assert row["scope"] == "agency"
