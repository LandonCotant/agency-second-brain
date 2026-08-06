"""First runtime caller of ``VECTOR_SEARCH`` (ADR 0038 §5).

Loads the top-K Areas/Resources notes semantically related to today's
themes (account names, decision titles, voice memo first-lines, risk
flag patterns) so REFLECT-mode can surface them in the Doc body
("Areas context" section).

Pattern:
  1. Build a single ``theme_query`` string from today's signals.
  2. Embed it via ``text-embedding-005`` (same model the corpus uses
     per ADR 0038 §1; lift ``notes_ingestor.embedder.VertexEmbedder``).
  3. Issue a ``VECTOR_SEARCH`` against
     ``agent_outputs.notes WHERE note_kind IN ('area','resource')``,
     filtering out HIPAA-isolated rows AND yesterday's Reflection Docs
     (echo-chamber risk; ADR 0044 §9 risk #3).
  4. Return top-K ``AreaNoteSnippet`` with truncated content.

Empty-corpus / no-themes / SDK error all degrade to ``[]``; the agent
elides the section instead of breaking.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ..notes_ingestor.embedder import DEFAULT_MODEL as DEFAULT_EMBED_MODEL
from ..notes_ingestor.embedder import Embedder

log = logging.getLogger("agency_brain.agents.evening_reflection.areas_context_reader")


DEFAULT_TOP_K = 3
DEFAULT_CHUNK_CHARS = 400
# Mirror knowledge_surfacer's relevance floor so an unrelated theme query
# doesn't surface arbitrary notes as "Areas context". cosine_threshold 0.6
# → max cosine distance 0.4.
DEFAULT_COSINE_THRESHOLD = 0.6
"""How many chars of ``markdown_content`` to surface per chunk in the
Doc body. Keeps the section readable; full source is one Drive click
away via the file's webViewLink."""


@dataclass(frozen=True)
class AreaNoteSnippet:
    """One ``agent_outputs.notes`` row from the Areas/Resources corpus."""

    note_id: str
    filename: str
    snippet: str
    """First ``DEFAULT_CHUNK_CHARS`` of ``markdown_content`` (rendered)."""
    distance: float
    """Cosine distance from the theme query (lower = more similar)."""
    source_drive_url: str | None = None


class BQQueryClient(Protocol):
    """Minimal BQ surface — parameterized SELECT.

    Mirrors ``writer.BQDedupClient`` ergonomics. Production wires
    ``main._BigQueryParameterizedAdapter``; tests pass a fake that
    returns canned rows.
    """

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class AreasContextReader:
    """Reads top-K Areas/Resources notes related to today's themes."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        embedder: Embedder,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "notes",
        embed_model: str = DEFAULT_EMBED_MODEL,
        top_k: int = DEFAULT_TOP_K,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        min_query_chars: int = 16,
        cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
    ) -> None:
        self._bq = bq_client
        self._embedder = embedder
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"
        self._embed_model = embed_model
        self._top_k = top_k
        self._chunk_chars = chunk_chars
        self._min_query_chars = min_query_chars
        self._cosine_threshold = cosine_threshold

    def load(self, theme_seeds: Iterable[str]) -> list[AreaNoteSnippet]:
        """Embed ``theme_seeds`` (joined) → VECTOR_SEARCH → top-K snippets.

        Empty seeds (or below ``min_query_chars`` after joining) → ``[]``
        without an embed call. Embed / BQ failure → ``[]`` with a
        logged exception (the agent elides the section).
        """
        query_text = self._build_query_text(theme_seeds)
        if len(query_text) < self._min_query_chars:
            log.info(
                "areas_context.skip: query_text too short (%d < %d chars)",
                len(query_text),
                self._min_query_chars,
            )
            return []

        try:
            vector = self._embedder.embed(text=query_text, model=self._embed_model)
        except Exception:
            log.exception("areas_context.embed_failed")
            return []
        if not vector:
            log.info("areas_context.embed_returned_empty: skipping VECTOR_SEARCH")
            return []

        sql = self._build_sql()
        max_distance = max(0.0, min(2.0, 1.0 - self._cosine_threshold))
        params = [
            {"name": "query_embedding", "type": "ARRAY_FLOAT64", "value": list(vector)},
            {"name": "top_k", "type": "INT64", "value": self._top_k},
            {"name": "max_distance", "type": "FLOAT64", "value": max_distance},
        ]
        try:
            rows = self._bq.query_rows(sql, parameters=params)
        except Exception:
            log.exception("areas_context.vector_search_failed")
            return []

        return [self._row_to_snippet(r) for r in rows]

    # -------------------------------------------------------------- helpers

    def _build_query_text(self, theme_seeds: Iterable[str]) -> str:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in theme_seeds:
            if not raw:
                continue
            s = " ".join(str(raw).split()).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            cleaned.append(s)
        # Cap the query at ~1500 chars so the embedder gets a focused signal.
        return ". ".join(cleaned)[:1500]

    def _build_sql(self) -> str:
        # Per ADR 0038 §2 — VECTOR_SEARCH against the embedded corpus
        # with cosine distance. Pre-filter via a subquery so VECTOR_SEARCH
        # only sees rows of the expected dimension (its dimension check
        # runs BEFORE post-WHERE filtering on the base table). Filters:
        #   - 768-dim embedding (text-embedding-005)
        #   - hipaa_isolated=FALSE
        #   - reference kinds only (area / resource)
        # Echo-chamber guard: exclude prior Reflection Docs (filename
        # ends with " Reflection.gdoc" / .docx variants) so tonight's
        # reflection doesn't surface yesterday's commentary as RAG.
        # Scope filter dropped vs Phase C v1: post-Phase-G client notes
        # land with scope='agency', and they're useful to surface in
        # the same Areas-context section.
        return f"""
WITH search AS (
  SELECT base.note_id,
         base.filename,
         base.markdown_content,
         base.source_drive_url,
         distance
  FROM VECTOR_SEARCH(
    (
      SELECT *
      FROM `{self._table_ref}`
      WHERE ARRAY_LENGTH(embedding) = 768
        AND hipaa_isolated = FALSE
        AND note_kind IN ('area','resource')
        AND LOWER(filename) NOT LIKE '% reflection.gdoc'
        AND LOWER(filename) NOT LIKE '% reflection.docx'
    ),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => @top_k,
    distance_type => 'COSINE'
  )
)
SELECT note_id, filename, markdown_content, source_drive_url, distance
FROM search
WHERE distance <= @max_distance
ORDER BY distance ASC
"""  # noqa: S608 — table_ref derived from constructor args; no user input

    def _row_to_snippet(self, row: dict) -> AreaNoteSnippet:
        markdown = (row.get("markdown_content") or "").strip()
        snippet = markdown[: self._chunk_chars]
        if len(markdown) > self._chunk_chars:
            snippet = snippet.rsplit(" ", 1)[0] + "…"
        return AreaNoteSnippet(
            note_id=str(row.get("note_id") or ""),
            filename=str(row.get("filename") or ""),
            snippet=snippet,
            distance=float(row.get("distance") or 0.0),
            source_drive_url=row.get("source_drive_url") or None,
        )


def build_theme_seeds(
    *,
    triaged_today,
    voice_memos,
    active_risk_flags,
    in_flight_decisions=None,
) -> list[str]:
    """Construct theme seeds from today's signals.

    Pulls account names + decision titles + voice-memo first-lines +
    risk flag patterns into a flat list. Order matters: stronger
    signals first so even a truncated query keeps them.
    """
    seeds: list[str] = []
    for it in triaged_today or ():
        summary = (getattr(it, "summary", None) or "").strip()
        if summary:
            seeds.append(summary)
        source = (getattr(it, "source", None) or "").strip()
        if source and source not in seeds:
            seeds.append(source)
    for flag in active_risk_flags or ():
        pat = (getattr(flag, "pattern_name", None) or "").strip()
        acct = (getattr(flag, "account_name", None) or "").strip()
        if acct and pat:
            seeds.append(f"{acct} {pat}")
        elif acct:
            seeds.append(acct)
        elif pat:
            seeds.append(pat)
    for d in in_flight_decisions or ():
        title = (getattr(d, "title", None) or "").strip()
        if title:
            seeds.append(title)
    for memo in voice_memos or ():
        body = (getattr(memo, "markdown_content", None) or "").strip()
        if body:
            seeds.append(body[:200])
    return seeds
