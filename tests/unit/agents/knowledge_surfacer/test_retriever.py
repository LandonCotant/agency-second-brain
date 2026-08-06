"""Tests for ``Retriever`` — embedding + VECTOR_SEARCH SQL shape."""

from __future__ import annotations

from agency_brain.agents.knowledge_surfacer.retriever import (
    DEFAULT_DIMS,
    INCLUDED_NOTE_KINDS,
    Retriever,
    tokenize_query,
)


class _FakeBQQuery:
    def __init__(self, *, rows: list[dict]) -> None:
        self.rows = list(rows)
        self.last_sql: str | None = None
        self.last_parameters: list[dict] | None = None

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        self.last_parameters = parameters
        return list(self.rows)


class _FakeEmbedder:
    def __init__(self, *, vector: list[float] | None) -> None:
        self.vector = vector
        self.calls: list[tuple[str, str]] = []

    def embed(self, *, text: str, model: str) -> list[float]:
        self.calls.append((text, model))
        if self.vector is None:
            raise RuntimeError("embedder failure")
        return list(self.vector)


def _make_retriever(
    *,
    rows: list[dict],
    vector: list[float] | None = None,
    top_k: int = 6,
    cosine_threshold: float = 0.65,
    half_life_days: float = 0.0,
    hybrid: bool = False,
) -> tuple[Retriever, _FakeBQQuery, _FakeEmbedder]:
    embedding = vector if vector is not None else [0.1] * DEFAULT_DIMS
    bq = _FakeBQQuery(rows=rows)
    embedder = _FakeEmbedder(vector=embedding)
    retriever = Retriever(
        bq_query=bq,
        embedder=embedder,
        project_id="p",
        top_k=top_k,
        cosine_threshold=cosine_threshold,
        half_life_days=half_life_days,
        hybrid=hybrid,
    )
    return retriever, bq, embedder


def test_retrieve_returns_chunks_with_metadata() -> None:
    rows = [
        {
            "note_id": "n1",
            "filename": "morning_brief_design.md",
            "source_drive_url": "https://drive/n1",
            "markdown_content": "Morning brief uses VECTOR_SEARCH on agent_outputs.notes",
            "scope": "agency",
            "note_kind": "area",
            "hipaa_isolated": False,
            "event_metadata_json": None,
            "distance": 0.12,
        },
    ]
    retriever, bq, embedder = _make_retriever(rows=rows)

    chunks = retriever.retrieve(query="how does VECTOR_SEARCH work?")

    assert embedder.calls == [("how does VECTOR_SEARCH work?", "text-embedding-005")]
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.note_id == "n1"
    assert chunk.filename == "morning_brief_design.md"
    assert chunk.scope == "agency"
    assert chunk.note_kind == "area"
    assert chunk.hipaa_isolated is False
    assert "VECTOR_SEARCH" in chunk.markdown_excerpt


def test_retrieve_sql_carries_load_bearing_filters() -> None:
    """The retriever SQL MUST keep the four invariants (one missing
    is a HIPAA / dimension / kind regression)."""
    retriever, bq, _ = _make_retriever(rows=[])
    retriever.retrieve(query="anything")
    assert bq.last_sql is not None
    sql = bq.last_sql
    assert "ARRAY_LENGTH(embedding) = 768" in sql
    assert "hipaa_isolated = FALSE" in sql
    assert "note_kind IN UNNEST(@included_kinds)" in sql
    assert "VECTOR_SEARCH" in sql
    assert "distance_type => 'COSINE'" in sql


def test_retrieve_passes_included_note_kinds() -> None:
    retriever, bq, _ = _make_retriever(rows=[])
    retriever.retrieve(query="anything")
    assert bq.last_parameters is not None
    kinds_param = next(p for p in bq.last_parameters if p["name"] == "included_kinds")
    assert kinds_param["type"] == "ARRAY_STRING"
    assert set(kinds_param["value"]) == set(INCLUDED_NOTE_KINDS)
    # ``archive`` stays excluded — archived content is cold storage, not a
    # fresh signal to surface in brain_ask. ``resource`` IS now included
    # per ADR 0054 (templates / frameworks ground "what template do I
    # have" style asks).
    assert "archive" not in kinds_param["value"]
    assert "resource" in kinds_param["value"]


def test_included_note_kinds_covers_adr_0052_additions() -> None:
    """ADR 0052: capture / decision / win were added to the allowed
    set so brain_ask finds synthetic notes + previously-invisible
    capture_note writes."""
    assert "capture" in INCLUDED_NOTE_KINDS
    assert "decision" in INCLUDED_NOTE_KINDS
    assert "win" in INCLUDED_NOTE_KINDS
    # Pre-ADR-0052 set still present (no regression).
    for kind in ("inbox", "area", "galaxy", "calendar_event"):
        assert kind in INCLUDED_NOTE_KINDS
    # ADR 0054 — ``resource`` widened in. ``archive`` stays excluded.
    assert "resource" in INCLUDED_NOTE_KINDS
    assert "archive" not in INCLUDED_NOTE_KINDS


def test_retrieve_empty_query_short_circuits() -> None:
    retriever, bq, embedder = _make_retriever(rows=[])
    assert retriever.retrieve(query="") == []
    assert retriever.retrieve(query="   ") == []
    assert embedder.calls == []
    assert bq.last_sql is None


def test_retrieve_handles_embedder_failure() -> None:
    bq = _FakeBQQuery(rows=[])
    embedder = _FakeEmbedder(vector=None)  # raises on embed()
    retriever = Retriever(bq_query=bq, embedder=embedder, project_id="p")
    assert retriever.retrieve(query="x") == []
    assert bq.last_sql is None  # never reached the SQL


def test_retrieve_handles_wrong_dim_embedding() -> None:
    bq = _FakeBQQuery(rows=[])
    embedder = _FakeEmbedder(vector=[0.0, 0.1])  # 2-dim, not 768
    retriever = Retriever(bq_query=bq, embedder=embedder, project_id="p")
    assert retriever.retrieve(query="x") == []
    assert bq.last_sql is None


def test_retrieve_truncates_markdown_excerpt() -> None:
    long_md = "X" * 5000
    rows = [
        {
            "note_id": "n1",
            "filename": "f.md",
            "source_drive_url": None,
            "markdown_content": long_md,
            "scope": "agency",
            "note_kind": "area",
            "hipaa_isolated": False,
            "event_metadata_json": None,
            "distance": 0.1,
        }
    ]
    retriever, _, _ = _make_retriever(rows=rows)
    chunks = retriever.retrieve(query="q")
    assert len(chunks[0].markdown_excerpt) <= 1501  # 1500 + ellipsis


def test_retrieve_max_distance_derives_from_threshold() -> None:
    retriever, bq, _ = _make_retriever(rows=[], cosine_threshold=0.65)
    retriever.retrieve(query="q")
    md = next(p for p in bq.last_parameters if p["name"] == "max_distance")
    # max_distance = 1 - threshold
    assert abs(md["value"] - 0.35) < 1e-9


def test_retrieve_recency_weighting_emits_rescore_sql() -> None:
    """With half_life > 0, SQL recomputes order via EXP(-age / half_life)."""
    retriever, bq, _ = _make_retriever(rows=[], half_life_days=30.0, top_k=8)
    retriever.retrieve(query="anything")
    sql = bq.last_sql or ""
    assert "recency_score" in sql
    assert "TIMESTAMP_DIFF" in sql
    assert "ORDER BY recency_score DESC" in sql
    assert "EXP(" in sql
    # Load-bearing filters preserved.
    assert "ARRAY_LENGTH(embedding) = 768" in sql
    assert "hipaa_isolated = FALSE" in sql
    # Over-fetch parameter present + correct value.
    oversample = next(p for p in bq.last_parameters if p["name"] == "oversample_top_k")
    assert oversample["value"] == 8 * 3  # OVERSAMPLE_FACTOR = 3
    half_life = next(p for p in bq.last_parameters if p["name"] == "half_life_days")
    assert abs(half_life["value"] - 30.0) < 1e-9


def test_retrieve_half_life_zero_disables_recency() -> None:
    """half_life_days <= 0 short-circuits to the pre-recency SQL shape."""
    retriever, bq, _ = _make_retriever(rows=[], half_life_days=0.0)
    retriever.retrieve(query="anything")
    sql = bq.last_sql or ""
    assert "recency_score" not in sql
    assert "TIMESTAMP_DIFF" not in sql
    assert "ORDER BY distance ASC" in sql
    # No oversample / half_life params bound when recency is off.
    names = {p["name"] for p in bq.last_parameters or []}
    assert "oversample_top_k" not in names
    assert "half_life_days" not in names


def test_retrieve_recency_default_off_preserves_backcompat() -> None:
    """Class default is half_life=0 — recency OFF to preserve the
    Cloud Run Knowledge Surfacer's pre-existing behavior. MCP brain_ask
    opts in via cfg.half_life_days."""
    bq = _FakeBQQuery(rows=[])
    embedder = _FakeEmbedder(vector=[0.1] * DEFAULT_DIMS)
    # No half_life_days kwarg — default 0, recency OFF.
    retriever = Retriever(bq_query=bq, embedder=embedder, project_id="p")
    retriever.retrieve(query="anything")
    assert "recency_score" not in (bq.last_sql or "")
    assert "ORDER BY distance ASC" in (bq.last_sql or "")


def test_hybrid_default_off_uses_pure_vector_sql() -> None:
    """Class default is hybrid=False — pure VECTOR_SEARCH, no SEARCH() arm."""
    retriever, bq, _ = _make_retriever(rows=[])  # hybrid defaults False
    retriever.retrieve(query="Client A renewal")
    sql = bq.last_sql or ""
    assert "SEARCH(markdown_content" not in sql  # no keyword arm
    assert "rrf_score" not in sql
    assert "VECTOR_SEARCH" in sql


# ------------------------------ ADR 0068 hybrid -----------------------------


def test_hybrid_sql_fuses_vector_and_keyword_with_rrf() -> None:
    """Hybrid SQL must carry BOTH arms + the RRF fusion expression."""
    retriever, bq, _ = _make_retriever(rows=[], hybrid=True, half_life_days=30.0)
    retriever.retrieve(query="Client A renewal date")
    sql = bq.last_sql or ""
    # Both retrieval arms present. Keyword arm uses per-term scalar params
    # (@kw0, …) because BQ SEARCH() needs a constant 2nd argument.
    assert "VECTOR_SEARCH" in sql
    assert "SEARCH(markdown_content, @kw0)" in sql
    assert "SEARCH(filename, @kw0)" in sql
    # RRF fusion + per-arm ranks.
    assert "1.0 / (@rrf_k + vec.vec_rank)" in sql
    assert "1.0 / (@rrf_k + kw.kw_rank)" in sql
    assert "FULL OUTER JOIN kw" in sql
    assert "AS rrf_score" in sql
    assert "AS match_type" in sql
    # Recency enters as a THIRD RRF channel (rank), not a multiplier — RRF
    # scores are compressed near 1/k, so multiplying by EXP(decay) would let
    # recency dominate the ranking.
    assert "base_rrf + 1.0 / (@rrf_k + rec_rank)" in sql
    assert "rec_rank" in sql
    assert "rrf_score * EXP(" not in sql


def test_hybrid_sql_preserves_invariants_in_both_arms() -> None:
    """The four load-bearing invariants must appear in BOTH the vector
    and keyword arms (one missing = HIPAA / dimension / kind regression)."""
    retriever, bq, _ = _make_retriever(rows=[], hybrid=True)
    retriever.retrieve(query="renewal")
    sql = bq.last_sql or ""
    # Each filter appears once per arm → at least twice.
    assert sql.count("ARRAY_LENGTH(embedding) = 768") >= 2
    assert sql.count("hipaa_isolated = FALSE") >= 2
    assert sql.count("note_kind IN UNNEST(@included_kinds)") >= 2
    # COSINE still drives the vector arm.
    assert "distance_type => 'COSINE'" in sql


def test_hybrid_binds_query_terms_and_rrf_params() -> None:
    retriever, bq, _ = _make_retriever(rows=[], hybrid=True, top_k=8)
    retriever.retrieve(query="Acme Corp Q3 invoice")
    params = {p["name"]: p for p in (bq.last_parameters or [])}
    # One scalar STRING param per token (stopwords dropped, lowercased).
    assert params["kw0"]["value"] == "acme"
    assert params["kw1"]["value"] == "corp"
    assert params["kw2"]["value"] == "q3"
    assert params["kw3"]["value"] == "invoice"
    assert all(params[f"kw{i}"]["type"] == "STRING" for i in range(4))
    assert "kw4" not in params  # exactly four tokens
    assert params["rrf_k"]["value"] == 60
    assert params["cand_k"]["value"] == 8 * 3  # OVERSAMPLE_FACTOR
    assert params["query_embedding"]["type"] == "ARRAY_FLOAT64"


def test_hybrid_row_shape_marks_match_type_and_keyword_distance() -> None:
    """A keyword-only fused row has NULL distance → similarity 0.0, and
    match_type/rrf_score flow through to the chunk."""
    rows = [
        {
            "note_id": "k1",
            "filename": "birthdays.md",
            "source_drive_url": None,
            "markdown_content": "Jane Doe — birthday March 3",
            "scope": "personal",
            "note_kind": "area",
            "hipaa_isolated": False,
            "event_metadata_json": None,
            "distance": None,  # keyword-only hit, no vector arm
            "match_type": "keyword",
            "rrf_score": 0.0163,
        },
    ]
    retriever, _, _ = _make_retriever(rows=rows, hybrid=True)
    chunks = retriever.retrieve(query="Jane birthday")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.match_type == "keyword"
    assert c.distance is None
    assert c.similarity == 0.0
    assert c.rrf_score == 0.0163


def test_hybrid_degrades_to_keyword_only_on_embed_failure() -> None:
    """With hybrid on, an embedding outage falls back to lexical recall
    instead of the pre-ADR-0068 black-out (return [])."""
    bq = _FakeBQQuery(rows=[])
    embedder = _FakeEmbedder(vector=None)  # raises on embed()
    retriever = Retriever(bq_query=bq, embedder=embedder, project_id="p", hybrid=True)
    retriever.retrieve(query="Client A renewal")
    sql = bq.last_sql or ""
    assert "VECTOR_SEARCH" not in sql  # no vector arm
    assert "SEARCH(markdown_content, @kw0)" in sql
    assert "'keyword' AS match_type" in sql


def test_hybrid_all_stopwords_query_falls_back_to_vector() -> None:
    """If tokenization yields no usable terms, hybrid can't run the
    keyword arm — fall through to pure vector (no SEARCH)."""
    retriever, bq, _ = _make_retriever(rows=[], hybrid=True)
    retriever.retrieve(query="what is it about")  # all stopwords
    sql = bq.last_sql or ""
    assert "SEARCH(markdown_content" not in sql  # no keyword arm
    assert "VECTOR_SEARCH" in sql


# ------------------------------ tokenizer -----------------------------------


def test_tokenize_drops_stopwords_short_tokens_and_dedups() -> None:
    assert tokenize_query("What did Client A say about the renewal?") == [
        "client",
        "renewal",
    ]
    # Order-preserving dedup.
    assert tokenize_query("renewal RENEWAL renewal date") == ["renewal", "date"]
    # Punctuation split, lowercase, drop 1-char tokens.
    assert tokenize_query("Q3-2026 invoice (#12)") == ["q3", "2026", "invoice", "12"]
    # All-stopword query → empty.
    assert tokenize_query("what is it about") == []
    assert tokenize_query("") == []
