"""Embedding + VECTOR_SEARCH retrieval for the Knowledge Surfacer (ADR 0046).

The SQL is structurally the Librarian's production VECTOR_SEARCH pattern
(``librarian/linker.py:189-214``) with two changes:

  1. ``note_kind IN ('inbox', 'area', 'galaxy', 'calendar_event',
     'capture', 'decision', 'win', 'resource')`` — excludes ``archive``
     (cold) only. ``resource`` was added in ADR 0054 so templates +
     frameworks ground ``brain_ask`` for questions like "what templates
     do I have for X?". ``calendar_event`` is forward-compatible for
     PR A2 / future work; a missing value is harmless.
  2. Cosine threshold defaults to 0.65 (env-tunable), lower than the
     linker's 0.78 — Surfacer optimizes for recall.

The load-bearing ``ARRAY_LENGTH(embedding) = 768`` pre-filter and
``hipaa_isolated = FALSE`` guard are preserved unchanged. Removing
either reopens the bug from ADR 0045 §9 (one length-0 row crashes the
function) AND/OR the HIPAA invariant from PRD §4.1 layer 2.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from ..notes_ingestor.embedder import (
    DEFAULT_DIMS,
    Embedder,
)
from ..notes_ingestor.embedder import (
    DEFAULT_MODEL as DEFAULT_EMBED_MODEL,
)
from .models import RetrievedChunk

log = logging.getLogger("agency_brain.agents.knowledge_surfacer.retriever")

DEFAULT_TOP_K = 6
DEFAULT_COSINE_THRESHOLD = 0.65
"""ADR 0046 §2 — recall over precision. Lower threshold than the
Librarian Linker (0.78) because the Surfacer wants any contextual hit,
not only tightly-related neighbors."""

DEFAULT_MARKDOWN_EXCERPT_CHARS = 1500
"""How much of each retrieved note's markdown to include in the
synthesis context. 1500 chars x top_k=6 ~= 9K chars ~= 2.5K tokens,
comfortably under the per-query budget."""

DEFAULT_HALF_LIFE_DAYS = 0.0
"""Recency half-life in days. A note ``N`` days old is reweighted by
``EXP(-N / half_life)`` — so a note one half-life old gets ~0.37x its
cosine score, two half-lives ~0.14x, etc. Class default is 0
(disabled / pure cosine) to preserve backward compatibility for the
Cloud Run Knowledge Surfacer caller; MCP ``brain_ask`` opts in via
``BRAIN_ASK_HALF_LIFE_DAYS`` (default 30)."""

OVERSAMPLE_FACTOR = 3
"""When recency weighting is on, fetch ``top_k * OVERSAMPLE_FACTOR``
candidates from VECTOR_SEARCH before rescoring + truncating. Lets a
recent-but-slightly-less-similar note overtake an older near-perfect
match within the same K-window."""

INCLUDED_NOTE_KINDS: tuple[str, ...] = (
    "inbox",
    "area",
    "galaxy",
    "calendar_event",
    # Additive widening per ADR 0052. ``capture`` was a latent bug —
    # ``capture_note`` writes were invisible to brain_ask before this
    # change (the tool's docstring promised retrievability but the
    # retriever filtered them out). ``decision``/``win`` are the
    # synthetic-notes pattern from ADR 0052: each write to
    # agent_outputs.decisions / .wins also writes a companion row here
    # so brain_ask retrieves them via the same VECTOR_SEARCH path.
    "capture",
    "decision",
    "win",
    # ADR 0054 — Resources (templates / references / prompts) are useful
    # grounding for brain_ask, e.g. "what discovery-call template do I
    # have?". Excluding them was a v0 assumption, not a hard constraint.
    # ``archive`` (cold) stays excluded; archived ≠ retrievable as a
    # fresh signal.
    "resource",
)

RRF_K_DEFAULT = 60
"""Reciprocal Rank Fusion constant (ADR 0068). The canonical k=60 from
Cormack et al. — large enough that the difference between rank 1 and
rank 2 is gentle, small enough that deep ranks still contribute little.
Fused score for a doc = sum over arms of ``1 / (k + rank)``."""

MIN_TOKEN_CHARS = 2
"""Drop 1-char tokens from the keyword arm — they match too broadly and
the search index analyzer discards them anyway."""

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "about",
        "what",
        "whats",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "which",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "their",
        "re",
        "vs",
        "tell",
        "find",
        "show",
        "say",
        "said",
        "know",
        "anything",
        "something",
    }
)
"""Generic English function words stripped before the keyword arm. Kept
deliberately small — proper nouns, client names, and domain terms (the
exact-match payload of the keyword channel) must survive tokenization."""

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize_query(query: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords + short tokens.

    Order-preserving dedup. This is the only non-SQL logic in the hybrid
    path (ADR 0068) — kept trivial so it ports 1:1 to the Cloudflare
    Worker (ADR 0067).
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(query.lower()):
        if len(tok) < MIN_TOKEN_CHARS or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _keyword_score_expr(query_terms: list[str]) -> tuple[str, list[dict]]:
    """Keyword score expression + bound params (ADR 0068).

    BigQuery ``SEARCH()`` requires its second argument to be a *constant
    expression* — a per-row value from ``UNNEST(@terms)`` is rejected at
    plan time. The terms are known at build time (tokenized in Python), so
    each is bound as its own scalar parameter ``@kw0, @kw1, …`` and the
    score is the count of terms whose token matches ``markdown_content`` or
    ``filename``. The TS Worker (ADR 0067) builds the same expression from
    the term list — still a trivial loop, no per-row dynamic SEARCH.
    """
    parts: list[str] = []
    params: list[dict] = []
    for i, term in enumerate(query_terms):
        name = f"kw{i}"
        parts.append(
            f"CAST(SEARCH(markdown_content, @{name}) " f"OR SEARCH(filename, @{name}) AS INT64)"
        )
        params.append({"name": name, "type": "STRING", "value": term})
    return (" + ".join(parts) if parts else "0"), params


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class Retriever:
    """Embed query, run VECTOR_SEARCH, return top-K chunks."""

    def __init__(
        self,
        *,
        bq_query: BQQueryClient,
        embedder: Embedder,
        project_id: str,
        notes_table: str = "notes",
        dataset_id: str = "agent_outputs",
        top_k: int = DEFAULT_TOP_K,
        cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
        excerpt_chars: int = DEFAULT_MARKDOWN_EXCERPT_CHARS,
        embed_model: str = DEFAULT_EMBED_MODEL,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        hybrid: bool = False,
        rrf_k: int = RRF_K_DEFAULT,
    ) -> None:
        self._bq_query = bq_query
        self._embedder = embedder
        self._notes_ref = f"{project_id}.{dataset_id}.{notes_table}"
        self._top_k = top_k
        self._cosine_threshold = cosine_threshold
        self._excerpt_chars = excerpt_chars
        self._embed_model = embed_model
        self._half_life_days = half_life_days
        # ADR 0068 — hybrid (vector + keyword, RRF-fused) retrieval.
        # Default OFF so the Cloud Run Knowledge Surfacer caller and any
        # other consumer keep pure-vector behavior; MCP brain_ask opts in
        # via cfg.hybrid_enabled.
        self._hybrid = hybrid
        self._rrf_k = max(1, rrf_k)

    def retrieve(self, *, query: str) -> list[RetrievedChunk]:
        """Embed ``query``, retrieve ranked chunks.

        Pure-vector by default; hybrid (vector + keyword, RRF-fused) when
        ``hybrid=True`` (ADR 0068). If hybrid is on but the embedding call
        fails or returns the wrong dimensionality, degrades to keyword-only
        rather than returning nothing — exact-match recall survives an
        embedding outage.
        """
        if not query or not query.strip():
            return []
        try:
            embedding = self._embedder.embed(text=query, model=self._embed_model)
        except Exception:
            log.exception("knowledge_surfacer.retriever.embed_failed")
            embedding = []
        has_embedding = bool(embedding) and len(embedding) == DEFAULT_DIMS
        if embedding and not has_embedding:
            log.warning(
                "knowledge_surfacer.retriever.bad_embedding_dims dims=%d",
                len(embedding),
            )

        query_terms = tokenize_query(query) if self._hybrid else []

        if self._hybrid and query_terms:
            if has_embedding:
                rows = self._hybrid_search(embedding=list(embedding), query_terms=query_terms)
            else:
                log.warning("knowledge_surfacer.retriever.degraded_keyword_only")
                rows = self._keyword_only_search(query_terms=query_terms)
        elif has_embedding:
            rows = self._vector_search(embedding=list(embedding))
        else:
            return []
        return [self._row_to_chunk(r) for r in rows]

    # -------------------------------------------------------------- helpers

    def _vector_search(self, *, embedding: list[float]) -> list[dict]:
        max_distance = max(0.0, min(2.0, 1.0 - self._cosine_threshold))
        use_recency = self._half_life_days > 0
        # When recency weighting is on, over-fetch from VECTOR_SEARCH so a
        # newer-but-slightly-less-similar note can overtake an older one
        # within the same K-window. When off, fetch exactly top_k (the
        # pre-recency behavior).
        oversample_top_k = self._top_k * OVERSAMPLE_FACTOR if use_recency else self._top_k
        if use_recency:
            sql = f"""
WITH search AS (
  SELECT base.note_id,
         base.filename,
         base.source_drive_url,
         base.markdown_content,
         base.scope,
         base.note_kind,
         base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json,
         base.created_at,
         base.ingested_at,
         distance
  FROM VECTOR_SEARCH(
    (
      SELECT *
      FROM `{self._notes_ref}`
      WHERE ARRAY_LENGTH(embedding) = 768
        AND hipaa_isolated = FALSE
        AND note_kind IN UNNEST(@included_kinds)
    ),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => @oversample_top_k,
    distance_type => 'COSINE'
  )
),
scored AS (
  SELECT *,
    (1.0 - distance) * EXP(
      - TIMESTAMP_DIFF(
          CURRENT_TIMESTAMP(),
          COALESCE(created_at, ingested_at),
          DAY
        ) / @half_life_days
    ) AS recency_score
  FROM search
  WHERE distance <= @max_distance
)
SELECT note_id, filename, source_drive_url, markdown_content,
       scope, note_kind, hipaa_isolated, event_metadata_json, distance
FROM scored
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY recency_score DESC) = 1
ORDER BY recency_score DESC
LIMIT @top_k
"""  # noqa: S608 — bound parameters, not f-string injection
        else:
            sql = f"""
WITH search AS (
  SELECT base.note_id,
         base.filename,
         base.source_drive_url,
         base.markdown_content,
         base.scope,
         base.note_kind,
         base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json,
         distance
  FROM VECTOR_SEARCH(
    (
      SELECT *
      FROM `{self._notes_ref}`
      WHERE ARRAY_LENGTH(embedding) = 768
        AND hipaa_isolated = FALSE
        AND note_kind IN UNNEST(@included_kinds)
    ),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => @top_k,
    distance_type => 'COSINE'
  )
)
SELECT note_id, filename, source_drive_url, markdown_content,
       scope, note_kind, hipaa_isolated, event_metadata_json, distance
FROM search
WHERE distance <= @max_distance
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY distance ASC) = 1
ORDER BY distance ASC
LIMIT @top_k
"""  # noqa: S608 — bound parameters, not f-string injection
        params: list[dict] = [
            {"name": "query_embedding", "type": "ARRAY_FLOAT64", "value": embedding},
            {"name": "top_k", "type": "INT64", "value": self._top_k},
            {"name": "max_distance", "type": "FLOAT64", "value": max_distance},
            {
                "name": "included_kinds",
                "type": "ARRAY_STRING",
                "value": list(INCLUDED_NOTE_KINDS),
            },
        ]
        if use_recency:
            params.append({"name": "oversample_top_k", "type": "INT64", "value": oversample_top_k})
            params.append(
                {
                    "name": "half_life_days",
                    "type": "FLOAT64",
                    "value": float(self._half_life_days),
                }
            )
        try:
            return self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("knowledge_surfacer.retriever.vector_search_failed")
            return []

    def _hybrid_search(self, *, embedding: list[float], query_terms: list[str]) -> list[dict]:
        """Vector + keyword arms fused via RRF, in one SQL query (ADR 0068).

        Both arms independently carry the four load-bearing invariants
        (768-dim, hipaa_isolated, included note_kinds, COSINE). BQ
        ``SEARCH()`` is boolean even with a search index, so the keyword
        arm scores by count of distinct matching query terms. Recency
        (when half_life > 0) enters as a *third RRF channel* — a recency
        rank over the fused candidate union — NOT a multiplier: RRF scores
        are compressed near ``1/k``, so multiplying them by an exponential
        decay lets recency dominate and crushes older-but-relevant hits.
        """
        max_distance = max(0.0, min(2.0, 1.0 - self._cosine_threshold))
        use_recency = self._half_life_days > 0
        cand_k = self._top_k * OVERSAMPLE_FACTOR
        final_score = "base_rrf + 1.0 / (@rrf_k + rec_rank)" if use_recency else "base_rrf"
        kw_score_expr, term_params = _keyword_score_expr(query_terms)
        sql = f"""
WITH vec AS (
  SELECT base.note_id, base.filename, base.source_drive_url,
         base.markdown_content, base.scope, base.note_kind,
         base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json,
         base.created_at, base.ingested_at, distance,
         ROW_NUMBER() OVER (ORDER BY distance ASC) AS vec_rank
  FROM VECTOR_SEARCH(
    (
      SELECT *
      FROM `{self._notes_ref}`
      WHERE ARRAY_LENGTH(embedding) = 768
        AND hipaa_isolated = FALSE
        AND note_kind IN UNNEST(@included_kinds)
    ),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => @cand_k,
    distance_type => 'COSINE'
  )
  WHERE distance <= @max_distance
),
kw AS (
  SELECT note_id, filename, source_drive_url, markdown_content,
         scope, note_kind, hipaa_isolated, event_metadata_json,
         created_at, ingested_at, kw_score,
         ROW_NUMBER() OVER (ORDER BY kw_score DESC, created_at DESC) AS kw_rank
  FROM (
    SELECT note_id, filename, source_drive_url, markdown_content,
           scope, note_kind, hipaa_isolated,
           TO_JSON_STRING(event_metadata) AS event_metadata_json,
           created_at, ingested_at,
           ({kw_score_expr}) AS kw_score
    FROM `{self._notes_ref}`
    WHERE ARRAY_LENGTH(embedding) = 768
      AND hipaa_isolated = FALSE
      AND note_kind IN UNNEST(@included_kinds)
  )
  WHERE kw_score > 0
  ORDER BY kw_score DESC, created_at DESC
  LIMIT @cand_k
),
fused AS (
  SELECT
    COALESCE(vec.note_id, kw.note_id) AS note_id,
    COALESCE(vec.filename, kw.filename) AS filename,
    COALESCE(vec.source_drive_url, kw.source_drive_url) AS source_drive_url,
    COALESCE(vec.markdown_content, kw.markdown_content) AS markdown_content,
    COALESCE(vec.scope, kw.scope) AS scope,
    COALESCE(vec.note_kind, kw.note_kind) AS note_kind,
    COALESCE(vec.hipaa_isolated, kw.hipaa_isolated) AS hipaa_isolated,
    COALESCE(vec.event_metadata_json, kw.event_metadata_json) AS event_metadata_json,
    COALESCE(vec.created_at, kw.created_at) AS created_at,
    COALESCE(vec.ingested_at, kw.ingested_at) AS ingested_at,
    vec.distance AS distance,
    CASE
      WHEN vec.note_id IS NOT NULL AND kw.note_id IS NOT NULL THEN 'both'
      WHEN vec.note_id IS NOT NULL THEN 'semantic'
      ELSE 'keyword'
    END AS match_type,
    (
      COALESCE(1.0 / (@rrf_k + vec.vec_rank), 0.0)
      + COALESCE(1.0 / (@rrf_k + kw.kw_rank), 0.0)
    ) AS base_rrf
  FROM vec
  FULL OUTER JOIN kw ON vec.note_id = kw.note_id
),
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      ORDER BY COALESCE(created_at, ingested_at) DESC
    ) AS rec_rank
  FROM fused
)
SELECT note_id, filename, source_drive_url, markdown_content,
       scope, note_kind, hipaa_isolated, event_metadata_json,
       distance, match_type, ({final_score}) AS rrf_score
FROM ranked
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY rrf_score DESC) = 1
ORDER BY rrf_score DESC
LIMIT @top_k
"""  # noqa: S608 — bound parameters + literal score expr, not injection
        params: list[dict] = [
            {"name": "query_embedding", "type": "ARRAY_FLOAT64", "value": embedding},
            {"name": "top_k", "type": "INT64", "value": self._top_k},
            {"name": "cand_k", "type": "INT64", "value": cand_k},
            {"name": "max_distance", "type": "FLOAT64", "value": max_distance},
            {"name": "rrf_k", "type": "INT64", "value": self._rrf_k},
            {
                "name": "included_kinds",
                "type": "ARRAY_STRING",
                "value": list(INCLUDED_NOTE_KINDS),
            },
            *term_params,
        ]
        if use_recency:
            params.append(
                {
                    "name": "half_life_days",
                    "type": "FLOAT64",
                    "value": float(self._half_life_days),
                }
            )
        try:
            return self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("knowledge_surfacer.retriever.hybrid_search_failed")
            return []

    def _keyword_only_search(self, *, query_terms: list[str]) -> list[dict]:
        """Lexical-only retrieval — ADR 0068 resilience path.

        Used when hybrid is on but the embedding is unavailable (today's
        non-hybrid path black-outs here). Ranks by matching-term count, with
        recency as a second RRF channel (rank by recency) when half_life > 0
        — same fusion shape as ``_hybrid_search`` minus the vector arm.
        Carries the same invariants; ``distance`` is NULL (no vector arm).
        """
        use_recency = self._half_life_days > 0
        final_score = (
            "1.0 / (@rrf_k + kw_rank) + 1.0 / (@rrf_k + rec_rank)"
            if use_recency
            else "1.0 / (@rrf_k + kw_rank)"
        )
        kw_score_expr, term_params = _keyword_score_expr(query_terms)
        sql = f"""
WITH kw AS (
  SELECT note_id, filename, source_drive_url, markdown_content,
         scope, note_kind, hipaa_isolated,
         TO_JSON_STRING(event_metadata) AS event_metadata_json,
         created_at, ingested_at,
         ({kw_score_expr}) AS kw_score
  FROM `{self._notes_ref}`
  WHERE ARRAY_LENGTH(embedding) = 768
    AND hipaa_isolated = FALSE
    AND note_kind IN UNNEST(@included_kinds)
),
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (ORDER BY kw_score DESC, created_at DESC) AS kw_rank,
    ROW_NUMBER() OVER (
      ORDER BY COALESCE(created_at, ingested_at) DESC
    ) AS rec_rank
  FROM kw
  WHERE kw_score > 0
)
SELECT note_id, filename, source_drive_url, markdown_content,
       scope, note_kind, hipaa_isolated, event_metadata_json,
       CAST(NULL AS FLOAT64) AS distance,
       'keyword' AS match_type,
       ({final_score}) AS rrf_score
FROM ranked
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY rrf_score DESC) = 1
ORDER BY rrf_score DESC
LIMIT @top_k
"""  # noqa: S608 — bound parameters + literal score expr, not injection
        params: list[dict] = [
            {"name": "top_k", "type": "INT64", "value": self._top_k},
            {"name": "rrf_k", "type": "INT64", "value": self._rrf_k},
            {
                "name": "included_kinds",
                "type": "ARRAY_STRING",
                "value": list(INCLUDED_NOTE_KINDS),
            },
            *term_params,
        ]
        try:
            return self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("knowledge_surfacer.retriever.keyword_search_failed")
            return []

    def _row_to_chunk(self, row: dict) -> RetrievedChunk:
        markdown = str(row.get("markdown_content") or "")
        if len(markdown) > self._excerpt_chars:
            markdown = markdown[: self._excerpt_chars] + "…"
        raw_distance = row.get("distance")
        raw_rrf = row.get("rrf_score")
        return RetrievedChunk(
            note_id=str(row.get("note_id") or ""),
            filename=str(row.get("filename") or ""),
            source_drive_url=row.get("source_drive_url") or None,
            markdown_excerpt=markdown,
            distance=float(raw_distance) if raw_distance is not None else None,
            scope=str(row.get("scope") or ""),
            note_kind=str(row.get("note_kind") or ""),
            hipaa_isolated=bool(row.get("hipaa_isolated") or False),
            event_metadata_json=row.get("event_metadata_json") or None,
            match_type=str(row.get("match_type") or "semantic"),
            rrf_score=float(raw_rrf) if raw_rrf is not None else None,
        )
