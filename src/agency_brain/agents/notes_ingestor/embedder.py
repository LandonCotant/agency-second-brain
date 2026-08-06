"""Vertex `text-embedding-005` wrapper for the notes ingestor (ADR 0038).

Single-purpose: turn `markdown_content` into a 768-dim vector and a
content hash that the writer can use for idempotent re-embed checks
(ADR 0038 §4).

Decoupled from the Vertex SDK by an ``Embedder`` Protocol so unit
tests can pass a fake. Production wires the real Vertex SDK in
``main.py``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Protocol

from .models import EmbeddingResult

log = logging.getLogger("agency_brain.agents.notes_ingestor.embedder")

DEFAULT_MODEL = "text-embedding-005"
DEFAULT_DIMS = 768


class Embedder(Protocol):
    """Generates a single embedding vector for a string of text."""

    def embed(self, *, text: str, model: str) -> list[float]: ...


class EmbedError(RuntimeError):
    """Raised when the embedder returns an unusable response."""


def content_hash(markdown: str) -> str:
    """SHA-256 hex digest of the markdown payload — embedding idempotency
    key per ADR 0038 §4."""
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()


def embed_markdown(
    *,
    markdown: str,
    embedder: Embedder,
    model: str = DEFAULT_MODEL,
) -> EmbeddingResult:
    """Embed `markdown` and return an ``EmbeddingResult``.

    Empty markdown short-circuits to an empty vector (ADR 0038 §3 — rows
    with `extraction_method='failed'` get NULL embedding by design).
    A failed embedder call raises ``EmbedError`` so the caller can fall
    back to writing the row without an embedding rather than dropping
    the entire ingest.
    """
    h = content_hash(markdown)
    if not (markdown or "").strip():
        return EmbeddingResult(vector=(), model=model, content_hash=h)

    try:
        vector = embedder.embed(text=markdown, model=model)
    except Exception as exc:
        log.exception("notes_ingestor.embed.failed")
        raise EmbedError(f"embedder raised: {type(exc).__name__}: {exc}") from exc

    if not vector:
        raise EmbedError("embedder returned empty vector for non-empty markdown")
    if len(vector) != DEFAULT_DIMS:
        # Loud signal for model rotation / config drift. ADR 0038 §6
        # reserves this as a re-embed trigger via UPDATE … SET
        # embedding_model = NULL, but unexpected dims at generate time
        # is still worth logging.
        log.warning(
            "notes_ingestor.embed.unexpected_dims model=%s dims=%d expected=%d",
            model,
            len(vector),
            DEFAULT_DIMS,
        )

    return EmbeddingResult(
        vector=tuple(float(x) for x in vector),
        model=model,
        content_hash=h,
    )


class VertexEmbedder:
    """Production ``Embedder`` wired to Vertex via ``google-genai``.

    Migrated from deprecated ``vertexai.language_models.TextEmbeddingModel``
    to ``google.genai.Client.models.embed_content`` on 2026-05-28 per audit
    F4 Phase 1. Mirrors ``extractor.VertexMultimodalLLM`` lazy-init pattern.
    """

    def __init__(self, *, project_id: str, location: str = "us-central1") -> None:
        self._project_id = project_id
        self._location = location
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project_id, location=self._location
            )
        return self._client

    def embed(self, *, text: str, model: str) -> list[float]:
        client = self._ensure_client()
        response = client.models.embed_content(model=model, contents=[text])
        if not response.embeddings:
            return []
        # ``embeddings[0].values`` is the list[float] vector — same shape
        # as the old SDK so downstream BQ ``embedding`` column unchanged.
        return list(response.embeddings[0].values)
