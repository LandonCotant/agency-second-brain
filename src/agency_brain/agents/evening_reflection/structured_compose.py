"""Vertex/Gemini ``response_schema`` client for REFLECT-mode extraction (ADR 0040 §5).

Mirrors ``agents/triage/vertex_classifier.py`` end-to-end — same SDK
(``google.genai``), same OpenAPI Schema Object subset, same
``thinking_budget=0`` posture. The only difference is the schema shape:
REFLECT-mode produces a ``commentary`` string + three nullable arrays
(``decisions``, ``wins``, ``todos``) instead of the flat triage payload.

Why a separate client (not extending the prose
``EveningReflectionComposer``):
``vertexai.GenerativeModel`` does not honor ``response_schema`` /
``thinking_config`` / ``model_armor_config`` reliably; ``google.genai``
does (this is the SDK Triage already validates against in prod). PROMPT
mode keeps using the prose composer; REFLECT mode loads this
client. ADR 0040 §5.

The client returns a ``ReflectExtractionPayload`` dataclass — the
composer hands extracted decisions/wins/todos straight to the dispatcher.
A trailing ``ParseError`` from this module means the LLM produced
unparseable JSON; the agent catches it and falls back to a prose-only
draft so the daily ritual still ships.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .models import (
    ExtractedDecision,
    ExtractedTodo,
    ExtractedWin,
    ReflectExtractionPayload,
)

log = logging.getLogger("agency_brain.agents.evening_reflection.structured_compose")


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"


# Schema for REFLECT-mode controlled generation. Mirrors the OpenAPI
# Schema Object subset Vertex's ``response_schema`` accepts (uppercase
# types; ``nullable: true`` rather than ``type: ["...", "null"]``;
# ``required`` listed at the top level only). Decisions/wins/todos arrays
# are intentionally not in ``required`` so empty days return commentary
# only — the composer / dispatcher tolerate empty arrays cleanly.
REFLECT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "commentary": {
            "type": "STRING",
            "description": (
                "Backward-looking 3-section prose body — the Reflection "
                "Doc body renders this verbatim. Stays in third person; "
                "no salutations."
            ),
        },
        "custom_questions": {
            "type": "ARRAY",
            "nullable": True,
            "description": (
                "2-3 day-specific reflection prompts tailored to today's "
                "inputs. Rendered into the Reflection Doc alongside the "
                "standard set (ADR 0044). Empty array on quiet days."
            ),
            "items": {"type": "STRING"},
        },
        "decisions": {
            "type": "ARRAY",
            "nullable": True,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "context": {"type": "STRING", "nullable": True},
                    "source_voice_note_id": {
                        "type": "STRING",
                        "nullable": True,
                        "description": (
                            "agent_outputs.notes.note_id of the voice memo "
                            "this decision was extracted from. NULL when "
                            "the decision synthesizes multiple sources."
                        ),
                    },
                },
                "required": ["title"],
            },
        },
        "wins": {
            "type": "ARRAY",
            "nullable": True,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "summary": {"type": "STRING", "nullable": True},
                    "source_voice_note_id": {"type": "STRING", "nullable": True},
                },
                "required": ["title"],
            },
        },
        "todos": {
            "type": "ARRAY",
            "nullable": True,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "body": {"type": "STRING"},
                    "source_voice_note_id": {"type": "STRING", "nullable": True},
                },
                "required": ["body"],
            },
        },
    },
    "required": ["commentary"],
}


class ParseError(ValueError):
    """LLM returned text that doesn't satisfy the schema after parsing."""


class StructuredLLMClient(Protocol):
    """Minimal LLM surface — ``generate(prompt, model) -> raw JSON text``.

    Mirrors how Triage's ``VertexClassifier.classify`` returns raw JSON
    text and lets the agent parse it. The Protocol shape keeps unit
    tests fast (a stub that returns a canned JSON string).
    """

    def generate(self, *, prompt: str, model: str) -> str: ...


@dataclass(frozen=True)
class StructuredComposeConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    temperature: float = 0.4
    max_output_tokens: int = 8192


class VertexStructuredComposeClient:
    """Real ``StructuredLLMClient`` impl backed by ``google.genai``.

    The SDK client is built lazily on first call so unit tests + cold
    start stay cheap.
    """

    def __init__(
        self,
        config: StructuredComposeConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client

    def generate(self, *, prompt: str, model: str | None = None) -> str:
        client = self._get_or_build_client()
        gen_config = self._build_generation_config()
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = client.models.generate_content(
            model=model or self._config.model,
            contents=contents,
            config=gen_config,
        )
        return _extract_text(response)

    # ------------------------------------------------------------------ helpers

    def _get_or_build_client(self) -> Any:
        if self._client is None:
            from google import genai  # imported lazily

            self._client = genai.Client(
                vertexai=True,
                project=self._config.project_id,
                location=self._config.location,
            )
        return self._client

    def _build_generation_config(self) -> Any:
        from google.genai import types as genai_types

        kwargs: dict[str, Any] = {
            "temperature": self._config.temperature,
            "max_output_tokens": self._config.max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": REFLECT_RESPONSE_SCHEMA,
        }
        thinking_config = getattr(genai_types, "ThinkingConfig", None)
        if thinking_config is not None:
            # Disable hidden thinking tokens for REFLECT — the prompt is
            # already structured and constrained; thinking budget eats
            # output tokens that would otherwise carry the JSON payload.
            kwargs["thinking_config"] = thinking_config(thinking_budget=0)
        return genai_types.GenerateContentConfig(**kwargs)


def parse_extraction_payload(raw_json: str) -> ReflectExtractionPayload:
    """Parse an LLM raw JSON string into a ``ReflectExtractionPayload``.

    Tolerant of optional / null array fields. Raises ``ParseError`` when
    the JSON doesn't decode at all or when the required ``commentary``
    field is missing — the agent's outer try/except catches it and
    falls back to a prose-only draft.
    """
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ParseError(f"LLM output is not a JSON object: {type(data).__name__}")
    commentary = data.get("commentary")
    if not isinstance(commentary, str) or not commentary.strip():
        raise ParseError("LLM output missing required 'commentary' string")

    decisions_raw = data.get("decisions") or []
    wins_raw = data.get("wins") or []
    todos_raw = data.get("todos") or []
    questions_raw = data.get("custom_questions") or []
    if not isinstance(decisions_raw, list):
        raise ParseError("'decisions' must be a list")
    if not isinstance(wins_raw, list):
        raise ParseError("'wins' must be a list")
    if not isinstance(todos_raw, list):
        raise ParseError("'todos' must be a list")
    if not isinstance(questions_raw, list):
        # Tolerant parse — if the LLM emits a non-list custom_questions,
        # drop it rather than blowing up the entire extraction. Doc body
        # falls back to the standard questions block alone.
        questions_raw = []

    decisions = tuple(_parse_decision(d) for d in decisions_raw if isinstance(d, dict))
    wins = tuple(_parse_win(w) for w in wins_raw if isinstance(w, dict))
    todos = tuple(_parse_todo(t) for t in todos_raw if isinstance(t, dict))
    custom_questions = tuple(_q.strip() for _q in (str(raw) for raw in questions_raw) if _q.strip())
    return ReflectExtractionPayload(
        commentary=commentary.strip(),
        decisions=decisions,
        wins=wins,
        todos=todos,
        custom_questions=custom_questions,
    )


def _parse_decision(raw: dict) -> ExtractedDecision:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ParseError("decision missing 'title'")
    return ExtractedDecision(
        title=title,
        context=_optional_str(raw.get("context")),
        source_voice_note_id=_optional_str(raw.get("source_voice_note_id")),
    )


def _parse_win(raw: dict) -> ExtractedWin:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ParseError("win missing 'title'")
    return ExtractedWin(
        title=title,
        summary=_optional_str(raw.get("summary")),
        source_voice_note_id=_optional_str(raw.get("source_voice_note_id")),
    )


def _parse_todo(raw: dict) -> ExtractedTodo:
    body = str(raw.get("body") or "").strip()
    if not body:
        raise ParseError("todo missing 'body'")
    return ExtractedTodo(
        body=body,
        source_voice_note_id=_optional_str(raw.get("source_voice_note_id")),
    )


def _optional_str(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _extract_text(response: Any) -> str:
    """Mirror ``triage.vertex_classifier._extract_text``.

    The ``google.genai`` SDK has a few response shapes depending on
    version; we handle ``parsed`` first (when the SDK validates against
    response_schema and returns Python objects), then ``text``, then
    ``candidates[*].content.parts[*].text``.
    """
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, str):
            return parsed
        try:
            return json.dumps(parsed)
        except TypeError:
            pass

    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text

    parts_text: list[str] = []
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if isinstance(t, str) and t:
                parts_text.append(t)
    if parts_text:
        return "".join(parts_text)

    return json.dumps({"_extract_failed": True, "_repr": repr(response)[:500]})
