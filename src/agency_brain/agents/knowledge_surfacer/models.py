"""Knowledge Surfacer dataclasses (ADR 0046).

Frozen dataclasses for parity with the rest of the codebase. The
``KnowledgeQuery`` carries an empty ``aspects`` list because BaseAgent's
HIPAA pre-flight is keyed on inputs that arrive with a HIPAA aspect
already attached. The Surfacer's HIPAA defense is at the retrieval
layer (``hipaa_isolated = FALSE`` + per-row assertion in
``guardrail.check_hipaa_invariant``); BaseAgent's pre-flight is a no-op
for free-text queries by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class KnowledgeQuery:
    """Input to ``KnowledgeSurfacerAgent.invoke``."""

    question: str
    asked_by_email: str
    requested_at: datetime
    aspects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievedChunk:
    """One row from the retrieval result.

    ``markdown_excerpt`` is the leading slice of the note's content used as
    synthesis context. ``event_metadata_json`` is non-null only for
    calendar-event chunks (PR A2 onward) — JSON-encoded
    ``{start, end, attendees, organizer, location, status}``.

    ADR 0068 — hybrid retrieval adds two fields. ``match_type`` records
    which arm(s) surfaced the chunk: ``"semantic"`` (vector only),
    ``"keyword"`` (lexical SEARCH only), or ``"both"``. ``rrf_score`` is
    the Reciprocal Rank Fusion score (None on the pure-vector path). For a
    keyword-only hit there is no cosine distance, so ``distance`` is None
    and ``similarity`` returns 0.0 — the chunk earned its place lexically,
    not semantically.
    """

    note_id: str
    filename: str
    source_drive_url: str | None
    markdown_excerpt: str
    distance: float | None
    scope: str
    note_kind: str
    hipaa_isolated: bool
    event_metadata_json: str | None = None
    match_type: str = "semantic"
    rrf_score: float | None = None

    @property
    def similarity(self) -> float:
        """Convenience: cosine similarity (= 1 - distance).

        Returns 0.0 for keyword-only hits, which carry no vector distance.
        """
        if self.distance is None:
            return 0.0
        return max(0.0, 1.0 - self.distance)


@dataclass(frozen=True)
class Citation:
    """One citation rendered in the Chat-card response."""

    note_id: str
    filename: str
    source_drive_url: str | None
    chunk_number: int  # 1-indexed; matches ``[N]`` markers in answer_markdown


@dataclass(frozen=True)
class SynthesisOutput:
    """Raw model output, parsed."""

    answer_markdown: str
    cited_note_ids: tuple[str, ...]
    confidence: float
    refused: bool
    refusal_reason: str | None
    model_used: str  # "gemini-2.5-flash" or "gemini-2.5-pro" if escalated


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of the post-synthesis entity-presence check."""

    passed: bool
    missing_entities: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class KnowledgeSurfacerResponse:
    """Final response returned to the Chat handler.

    ``confidence`` mirrors ``SynthesisOutput.confidence`` — BaseAgent reads
    this for the human-review-routed threshold (PRD §6.1, default 0.7).
    """

    answer: str
    citations: tuple[Citation, ...]
    confidence: float
    refused: bool
    refusal_reason: str | None
    model_used: str
    cost_usd: float | None = None
