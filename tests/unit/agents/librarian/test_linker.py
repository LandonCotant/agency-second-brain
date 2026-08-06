"""Tests for ``LibrarianLinker``."""

from __future__ import annotations

from agency_brain.agents.librarian.linker import (
    DEFAULT_COSINE_THRESHOLD,
    DEFAULT_TOP_K,
    LibrarianLinker,
)


class _FakeBQQuery:
    """Stub that maps SQL substring to a canned row list."""

    def __init__(self, *, plans: list[tuple[str, list[dict]]]) -> None:
        # plans = [(substring, rows), ...] — first matching substring wins.
        self._plans = plans
        self.last_calls: list[tuple[str, list[dict] | None]] = []

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_calls.append((sql, parameters))
        for needle, rows in self._plans:
            if needle in sql:
                return list(rows)
        return []


class _FakeBQWriter:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict]]] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.inserted.append((table_ref, list(rows)))
        return []


class _FakeDossierEditor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def update_related_section(self, *, doc_id: str, body_lines: list[str]):
        self.calls.append((doc_id, list(body_lines)))
        return {"doc_id": doc_id, "updated": True}


def _make_linker(
    *,
    plans: list[tuple[str, list[dict]]],
    dossier_editor: _FakeDossierEditor | None = None,
) -> tuple[LibrarianLinker, _FakeBQQuery, _FakeBQWriter, _FakeDossierEditor]:
    bq_query = _FakeBQQuery(plans=plans)
    bq_writer = _FakeBQWriter()
    editor = dossier_editor if dossier_editor is not None else _FakeDossierEditor()
    linker = LibrarianLinker(
        bq_query=bq_query,
        bq_writer=bq_writer,
        project_id="p",
        dossier_editor=editor,
    )
    return linker, bq_query, bq_writer, editor


def test_link_writes_bidirectional_rows_for_neighbors() -> None:
    plans = [
        # Notes lookup → return one row representing the moved file.
        (
            "WHERE source_drive_file_id =",
            [{"note_id": "src", "filename": "clienta-memo.md", "embedding": [0.1] * 768}],
        ),
        # VECTOR_SEARCH → 2 neighbors.
        (
            "FROM VECTOR_SEARCH",
            [
                {
                    "note_id": "n1",
                    "filename": "clienta-pi-dossier.gdoc",
                    "source_drive_url": "https://drive/n1",
                    "distance": 0.10,
                },
                {
                    "note_id": "n2",
                    "filename": "ls-onboarding.gdoc",
                    "source_drive_url": None,
                    "distance": 0.18,
                },
            ],
        ),
        # Existing-pairs lookup → empty (no dedup hits).
        ("FROM `p.agent_outputs.notes_links`", []),
    ]
    linker, bq_query, bq_writer, editor = _make_linker(plans=plans)
    outcome = linker.link_for_drive_file(
        drive_file_id="drive-1",
        dossier_doc_id="dossier-doc-1",
    )
    # Bidirectional inserts: 2 neighbors × 2 directions = 4 rows.
    assert len(bq_writer.inserted) == 1
    table_ref, rows = bq_writer.inserted[0]
    assert table_ref == "p.agent_outputs.notes_links"
    assert len(rows) == 4
    pairs = {(r["source_note_id"], r["target_note_id"]) for r in rows}
    assert pairs == {("src", "n1"), ("n1", "src"), ("src", "n2"), ("n2", "src")}
    # Schema columns only — no link_type / created_at fields.
    for r in rows:
        assert set(r.keys()) == {
            "source_note_id",
            "target_note_id",
            "similarity",
            "computed_at",
        }
    # Outcome bookkeeping reflects rows + dossier edit.
    assert outcome.neighbors_linked == 4
    assert outcome.related_section_updated is True
    assert outcome.dossier_doc_id == "dossier-doc-1"
    # Dossier editor saw the source filename + neighbors.
    assert editor.calls
    body_lines = editor.calls[0][1]
    assert "clienta-memo.md" in body_lines[0]
    assert any("clienta-pi-dossier.gdoc" in ln for ln in body_lines)


def test_link_dedups_existing_pairs() -> None:
    plans = [
        (
            "WHERE source_drive_file_id =",
            [{"note_id": "src", "filename": "x.md", "embedding": [0.1] * 768}],
        ),
        (
            "FROM VECTOR_SEARCH",
            [
                {
                    "note_id": "n1",
                    "filename": "y.md",
                    "source_drive_url": None,
                    "distance": 0.20,
                },
            ],
        ),
        # Existing pair already there → forward direction skipped.
        (
            "FROM `p.agent_outputs.notes_links`",
            [{"source_note_id": "src", "target_note_id": "n1"}],
        ),
    ]
    linker, _, bq_writer, _ = _make_linker(plans=plans)
    outcome = linker.link_for_drive_file(drive_file_id="d", dossier_doc_id=None)
    rows = bq_writer.inserted[0][1]
    pairs = {(r["source_note_id"], r["target_note_id"]) for r in rows}
    # forward (src → n1) was skipped; backward (n1 → src) still inserted.
    assert pairs == {("n1", "src")}
    # Outcome counts the inserted rows, not the deduped ones.
    assert outcome.neighbors_linked == 1


def test_link_skips_when_note_not_yet_in_corpus() -> None:
    plans = [("WHERE source_drive_file_id =", [])]
    linker, _, bq_writer, editor = _make_linker(plans=plans)
    outcome = linker.link_for_drive_file(drive_file_id="d", dossier_doc_id="dossier-1")
    assert outcome.neighbors_linked == 0
    assert outcome.related_section_updated is False
    assert bq_writer.inserted == []
    assert editor.calls == []


def test_link_no_neighbors_still_updates_dossier_with_self_only() -> None:
    plans = [
        (
            "WHERE source_drive_file_id =",
            [{"note_id": "src", "filename": "x.md", "embedding": [0.1] * 768}],
        ),
        ("FROM VECTOR_SEARCH", []),
    ]
    linker, _, bq_writer, editor = _make_linker(plans=plans)
    outcome = linker.link_for_drive_file(drive_file_id="d", dossier_doc_id="dossier-1")
    assert outcome.neighbors_linked == 0
    assert bq_writer.inserted == []
    # Dossier still updated — the source filename anchors the section.
    assert outcome.related_section_updated is True
    assert editor.calls[0][1] == ["x.md"]


def test_link_skips_dossier_when_no_doc_id() -> None:
    plans = [
        (
            "WHERE source_drive_file_id =",
            [{"note_id": "src", "filename": "x.md", "embedding": [0.1] * 768}],
        ),
        (
            "FROM VECTOR_SEARCH",
            [{"note_id": "n1", "filename": "y.md", "source_drive_url": None, "distance": 0.2}],
        ),
        ("FROM `p.agent_outputs.notes_links`", []),
    ]
    linker, _, _, editor = _make_linker(plans=plans)
    outcome = linker.link_for_drive_file(drive_file_id="d", dossier_doc_id=None)
    assert outcome.related_section_updated is False
    assert editor.calls == []


def test_link_handles_dossier_edit_failure() -> None:
    class _BoomEditor(_FakeDossierEditor):
        def update_related_section(self, **_):
            raise RuntimeError("docs api down")

    plans = [
        (
            "WHERE source_drive_file_id =",
            [{"note_id": "src", "filename": "x.md", "embedding": [0.1] * 768}],
        ),
        (
            "FROM VECTOR_SEARCH",
            [{"note_id": "n1", "filename": "y.md", "source_drive_url": None, "distance": 0.2}],
        ),
        ("FROM `p.agent_outputs.notes_links`", []),
    ]
    linker, _, bq_writer, _ = _make_linker(plans=plans, dossier_editor=_BoomEditor())
    outcome = linker.link_for_drive_file(drive_file_id="d", dossier_doc_id="dossier-1")
    # Links still written — the dossier edit is best-effort.
    assert len(bq_writer.inserted[0][1]) == 2
    assert outcome.related_section_updated is False


def test_default_top_k_and_threshold_match_adr() -> None:
    assert DEFAULT_TOP_K == 3
    assert DEFAULT_COSINE_THRESHOLD == 0.78
