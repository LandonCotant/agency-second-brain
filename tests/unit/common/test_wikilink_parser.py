"""Unit tests for the wikilink parser + edge writer (ADR 0053)."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.common.wikilink_parser import (
    extract_wikilinks,
    resolve_target,
    write_wikilink_edges,
)


class _FakeBQ:
    """Records every issued SQL + parameters."""

    def __init__(self, fixtures: list[tuple[str, list[dict]]] | None = None) -> None:
        self._fixtures = fixtures or []
        self.calls: list[tuple[str, list[dict]]] = []

    def __call__(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.calls.append((sql, parameters or []))
        for substr, rows in self._fixtures:
            if substr in sql:
                return rows
        return []


# ----------------------------- extract_wikilinks -----------------------------


def test_extract_wikilinks_basic() -> None:
    assert extract_wikilinks("Talked to [[Client A]] today") == ["Client A"]


def test_extract_wikilinks_pipe_alias_drops_display_text() -> None:
    """[[Title|Display]] returns the Title side; display is for rendering."""
    assert extract_wikilinks("Met with [[Client A|the firm]] briefly") == ["Client A"]


def test_extract_wikilinks_multiple_targets_dedup_and_order() -> None:
    md = "[[Alpha]] and [[Beta]] and [[Alpha]] again, plus [[Gamma]]."
    assert extract_wikilinks(md) == ["Alpha", "Beta", "Gamma"]


def test_extract_wikilinks_handles_whitespace() -> None:
    assert extract_wikilinks("Touch base on [[  Client A  ]] soon") == ["Client A"]


def test_extract_wikilinks_skips_empty_brackets() -> None:
    assert extract_wikilinks("Empty [[]] and [[ ]] should not match") == []


def test_extract_wikilinks_handles_empty_input() -> None:
    assert extract_wikilinks("") == []
    assert extract_wikilinks(None) == []  # type: ignore[arg-type]


def test_extract_wikilinks_ignores_single_brackets() -> None:
    """Markdown link syntax [text](url) and single [X] aren't wikilinks."""
    assert extract_wikilinks("[link](url) and [not-a-wikilink]") == []


def test_extract_wikilinks_multiline_with_punctuation() -> None:
    md = """Decision: switch to [[Peninsula]] strategy.

    Action items:
    - email [[Client A]]
    - update [[Strategy Doc]]
    """
    assert extract_wikilinks(md) == ["Peninsula", "Client A", "Strategy Doc"]


# ----------------------------- resolve_target --------------------------------


def test_resolve_target_finds_existing_note() -> None:
    bq = _FakeBQ(
        fixtures=[
            ("SELECT note_id FROM", [{"note_id": "note-clienta-123"}]),
        ]
    )
    note_id = resolve_target(
        title="Client A",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        query_rows=bq,
    )
    assert note_id == "note-clienta-123"
    # Verify the SELECT case-insensitively matches the extension-
    # stripped filename and applies the HIPAA filter.
    sql, params = bq.calls[0]
    assert "REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')" in sql
    assert "= LOWER(@title)" in sql
    assert "hipaa_isolated" in sql
    assert params[0]["value"] == "Client A"


def test_resolve_target_returns_none_when_no_match() -> None:
    bq = _FakeBQ(fixtures=[("SELECT note_id FROM", [])])
    assert (
        resolve_target(
            title="Nonexistent Note",
            project_id="proj",
            dataset_id="agent_outputs",
            notes_table="notes",
            query_rows=bq,
        )
        is None
    )


# ----------------------------- write_wikilink_edges --------------------------


def _resolver_fixtures(matches: dict[str, str]) -> list[tuple[str, list[dict]]]:
    """Per-title resolution fixtures aren't expressible with the current
    matcher (it returns the first fixture whose substring matches). For
    the writer tests we just answer based on whichever resolution SQL
    was issued; the writer's per-target sequence is enough to drive
    behavior."""
    return [("SELECT note_id FROM", [{"note_id": v}]) for v in matches.values()]


def test_write_wikilink_edges_no_matches_returns_zero_inserts() -> None:
    bq = _FakeBQ()
    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="No wikilinks in this content.",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=bq,
    )
    assert result == {
        "matched": 0,
        "resolved": 0,
        "inserted": 0,
        "skipped_duplicate": 0,
        "skipped_unresolved": 0,
    }
    assert bq.calls == []


def test_write_wikilink_edges_resolves_inserts_and_dedups() -> None:
    """End-to-end: 1 wikilink matches, resolves, inserts, returns counts."""
    bq = _FakeBQ()
    # Sequential fixtures: first SELECT (resolve_target) returns a match;
    # second SELECT (idempotency check) returns no existing edge.
    call_count = {"n": 0}

    def fake_query(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        bq.calls.append((sql, parameters or []))
        call_count["n"] += 1
        # First call: resolve_target → match
        if "REGEXP_REPLACE(filename" in sql:
            return [{"note_id": "note-target-1"}]
        # Second call: idempotency SELECT in notes_links → no existing
        if "WHERE source_note_id = @src AND target_note_id = @tgt" in sql:
            return []
        return []

    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="See [[Client A]] for details.",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=fake_query,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )
    assert result == {
        "matched": 1,
        "resolved": 1,
        "inserted": 1,
        "skipped_duplicate": 0,
        "skipped_unresolved": 0,
    }
    # Three calls: resolve + dedup-check + INSERT.
    assert len(bq.calls) == 3
    insert_sql, insert_params = bq.calls[2]
    assert "INSERT INTO" in insert_sql
    assert "link_type" in insert_sql
    assert "'wikilink'" in insert_sql
    pdict = {p["name"]: p["value"] for p in insert_params}
    assert pdict["src"] == "src-1"
    assert pdict["tgt"] == "note-target-1"


def test_write_wikilink_edges_skips_unresolved_targets() -> None:
    """If the target doesn't exist in notes, no edge row is written."""
    bq = _FakeBQ()

    def fake_query(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        bq.calls.append((sql, parameters or []))
        if "REGEXP_REPLACE(filename" in sql:
            return []  # no matching note
        return []

    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="Reference [[Phantom Note]] that doesn't exist.",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=fake_query,  # type: ignore[arg-type]
    )
    assert result["matched"] == 1
    assert result["skipped_unresolved"] == 1
    assert result["inserted"] == 0
    # Only the resolve SELECT happened; no INSERT, no dedup SELECT.
    assert len(bq.calls) == 1


def test_write_wikilink_edges_idempotent_on_existing_edge() -> None:
    """If the edge already exists, the writer skips INSERT but reports duplicate."""
    bq = _FakeBQ()

    def fake_query(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        bq.calls.append((sql, parameters or []))
        if "REGEXP_REPLACE(filename" in sql:
            return [{"note_id": "note-target"}]
        if "WHERE source_note_id = @src" in sql:
            return [{"source_note_id": "src-1"}]
        return []

    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="Already-linked [[Client A]] reference.",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=fake_query,  # type: ignore[arg-type]
    )
    assert result["inserted"] == 0
    assert result["skipped_duplicate"] == 1
    # Only resolve + dedup SELECT, no INSERT.
    assert len(bq.calls) == 2


def test_write_wikilink_edges_skips_self_reference() -> None:
    """A note referencing itself (rare) collapses without writing an edge."""
    bq = _FakeBQ()

    def fake_query(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        bq.calls.append((sql, parameters or []))
        if "REGEXP_REPLACE(filename" in sql:
            # Match resolves to the same note_id as the source.
            return [{"note_id": "src-1"}]
        return []

    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="Self-ref [[Same Note]].",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=fake_query,  # type: ignore[arg-type]
    )
    assert result["matched"] == 1
    assert result["skipped_duplicate"] == 1
    assert result["inserted"] == 0


def test_write_wikilink_edges_swallows_per_edge_failures() -> None:
    """A failed INSERT for one edge must not abort other edges or raise."""
    bq = _FakeBQ()

    def fake_query(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        bq.calls.append((sql, parameters or []))
        if "REGEXP_REPLACE(filename" in sql:
            # Different note_ids for "First" and "Second"
            title = next(p["value"] for p in (parameters or []) if p["name"] == "title")
            return [{"note_id": f"note-{title.lower()}"}]
        if "WHERE source_note_id = @src" in sql:
            return []  # no existing edge
        if "INSERT INTO" in sql:
            pdict = {p["name"]: p["value"] for p in (parameters or [])}
            if pdict.get("tgt") == "note-first":
                raise RuntimeError("BQ transient error on first edge")
            return []
        return []

    result = write_wikilink_edges(
        source_note_id="src-1",
        markdown_content="[[First]] and [[Second]].",
        project_id="proj",
        dataset_id="agent_outputs",
        notes_table="notes",
        links_table="notes_links",
        query_rows=fake_query,  # type: ignore[arg-type]
    )
    # First edge's INSERT raised; second edge succeeded.
    assert result["matched"] == 2
    assert result["resolved"] == 2
    assert result["inserted"] == 1
