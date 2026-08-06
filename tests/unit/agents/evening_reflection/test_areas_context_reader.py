"""Tests for ``AreasContextReader``."""

from __future__ import annotations

from agency_brain.agents.evening_reflection.areas_context_reader import (
    AreaNoteSnippet,
    AreasContextReader,
    build_theme_seeds,
)


class _FakeEmbedder:
    def __init__(
        self, *, vector: list[float] | None = None, raises: Exception | None = None
    ) -> None:
        self._vector = vector if vector is not None else [0.1] * 768
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def embed(self, *, text: str, model: str) -> list[float]:
        self.calls.append((text, model))
        if self._raises is not None:
            raise self._raises
        return self._vector


class _FakeBQ:
    def __init__(self, *, rows: list[dict] | None = None, raises: Exception | None = None) -> None:
        self._rows = rows or []
        self._raises = raises
        self.last_sql: str | None = None
        self.last_params: list[dict] | None = None

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        self.last_params = parameters
        if self._raises is not None:
            raise self._raises
        return list(self._rows)


def _make_reader(
    *,
    embedder: _FakeEmbedder | None = None,
    bq: _FakeBQ | None = None,
    top_k: int = 3,
    chunk_chars: int = 80,
    min_query_chars: int = 8,
) -> tuple[AreasContextReader, _FakeEmbedder, _FakeBQ]:
    embedder = embedder or _FakeEmbedder()
    bq = bq or _FakeBQ()
    reader = AreasContextReader(
        bq_client=bq,
        embedder=embedder,
        project_id="test-project",
        top_k=top_k,
        chunk_chars=chunk_chars,
        min_query_chars=min_query_chars,
    )
    return reader, embedder, bq


def test_load_returns_top_k_snippets() -> None:
    bq = _FakeBQ(
        rows=[
            {
                "note_id": "n1",
                "filename": "clienta-pi-dossier.gdoc",
                "markdown_content": "Client A is a private investigation firm in Vegas.",
                "source_drive_url": "https://drive/n1",
                "distance": 0.21,
            },
            {
                "note_id": "n2",
                "filename": "ls-onboarding.gdoc",
                "markdown_content": "Local service onboarding playbook v1.",
                "source_drive_url": None,
                "distance": 0.27,
            },
        ]
    )
    reader, embedder, bq2 = _make_reader(bq=bq)
    snippets = reader.load(["Client A lead-gen renewal", "Client C Studio brief"])
    assert len(snippets) == 2
    assert isinstance(snippets[0], AreaNoteSnippet)
    assert snippets[0].note_id == "n1"
    assert "private investigation" in snippets[0].snippet
    assert snippets[0].distance == 0.21
    # Embedder was invoked once with the joined query text.
    assert len(embedder.calls) == 1
    assert "Client A" in embedder.calls[0][0]
    assert "Client C" in embedder.calls[0][0]
    # BQ params include the embedding + top_k + the relevance floor.
    names = {p["name"] for p in (bq2.last_params or [])}
    assert names == {"query_embedding", "top_k", "max_distance"}


def test_load_applies_distance_floor_in_sql_and_params() -> None:
    reader, _embedder, bq = _make_reader()
    reader.load(["Client A lead-gen renewal", "Client C Studio brief"])
    assert "WHERE distance <= @max_distance" in (bq.last_sql or "")
    params = {p["name"]: p["value"] for p in (bq.last_params or [])}
    # cosine_threshold 0.6 → max cosine distance 0.4.
    assert abs(params["max_distance"] - 0.4) < 1e-9


def test_load_short_query_skips_embed_and_bq() -> None:
    reader, embedder, bq = _make_reader(min_query_chars=20)
    snippets = reader.load(["hi"])  # too short
    assert snippets == []
    assert embedder.calls == []
    assert bq.last_sql is None


def test_load_empty_seeds_skips() -> None:
    reader, embedder, bq = _make_reader()
    assert reader.load([]) == []
    assert embedder.calls == []


def test_load_embed_failure_returns_empty() -> None:
    embedder = _FakeEmbedder(raises=RuntimeError("vertex unreachable"))
    reader, _, bq = _make_reader(embedder=embedder)
    assert reader.load(["Client C Studio brief work"]) == []
    assert bq.last_sql is None


def test_load_bq_failure_returns_empty() -> None:
    bq = _FakeBQ(raises=RuntimeError("BQ error"))
    reader, _, _ = _make_reader(bq=bq)
    assert reader.load(["Client C Studio brief work"]) == []


def test_snippet_truncates_long_markdown() -> None:
    long = "alpha bravo charlie delta echo " * 20  # ~600 chars
    bq = _FakeBQ(
        rows=[
            {
                "note_id": "n1",
                "filename": "f.gdoc",
                "markdown_content": long,
                "source_drive_url": None,
                "distance": 0.5,
            }
        ]
    )
    reader, _, _ = _make_reader(bq=bq, chunk_chars=80)
    snippets = reader.load(["alpha bravo charlie delta"])
    assert len(snippets[0].snippet) <= 81  # +1 for the ellipsis char
    assert snippets[0].snippet.endswith("…")


def test_sql_filters_reflections_and_hipaa() -> None:
    reader, _, bq = _make_reader()
    reader.load(["Some theme that is long enough"])
    sql = bq.last_sql or ""
    assert "note_kind IN ('area','resource')" in sql
    assert "hipaa_isolated = FALSE" in sql
    assert "reflection.gdoc" in sql.lower()


def test_build_theme_seeds_orders_strongest_signals_first() -> None:
    class _Triaged:
        def __init__(self, summary, source):
            self.summary, self.source = summary, source

    class _Risk:
        def __init__(self, pattern_name, account_name=None):
            self.pattern_name = pattern_name
            self.account_name = account_name

    class _Memo:
        def __init__(self, body):
            self.markdown_content = body

    class _Decision:
        def __init__(self, title):
            self.title = title

    seeds = build_theme_seeds(
        triaged_today=[_Triaged("Client A deliverable", "gmail")],
        voice_memos=[_Memo("Thinking about Client C brief and the Tuesday deadline")],
        active_risk_flags=[_Risk("Acknowledgment Gap", "Client C Studio")],
        in_flight_decisions=[_Decision("Move Client C deadline to Tuesday")],
    )
    # Triaged summary comes first (strongest signal).
    assert seeds[0] == "Client A deliverable"
    # Risk flag concatenates account + pattern.
    assert any("Acknowledgment Gap" in s and "Client C" in s for s in seeds)
    # Decision title surfaces.
    assert any("Move Client C deadline" in s for s in seeds)
    # Voice memo comes last (capped at 200 chars).
    assert any(s.startswith("Thinking about Client C") for s in seeds)
