"""Brag Spotter composer — Vertex Gemini 2.5 Flash + response_schema (ADR 0043 §4).

Mirrors `evening_reflection.structured_compose` end-to-end: same
`google.genai.Client(vertexai=True)`, same OpenAPI Schema Object subset,
same `thinking_budget=0` posture. The schema shape differs: Brag
Spotter emits `commentary` + `candidates` (a list of WinCandidate
shapes), Reflection v2 emits `commentary` + `decisions/wins/todos`.

The composer:
  1. Renders 5 source blocks + an "already captured" block from existing
     wins.
  2. Substitutes into `prompts/brag_spotter/v1.md`.
  3. Calls Vertex via `StructuredLLMClient.generate(prompt) -> raw JSON`.
  4. Parses into `BragSpotterPayload` (drops candidates whose
     `source_kind` isn't on the allowlist).

A `ParseError` from this module means the LLM produced unparseable
JSON; the agent catches it and falls back to a "Quiet week — no wins
flagged" digest so the Sunday ritual still ships.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .models import (
    VALID_SOURCE_KINDS,
    BragSpotterPayload,
    DecisionRow,
    ExistingWinRow,
    NoteRow,
    ReflectionRow,
    RoutedEventRow,
    TriagedItemRow,
    WinCandidate,
)

log = logging.getLogger("agency_brain.agents.brag_spotter.composer")


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"


BRAG_SPOTTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "commentary": {
            "type": "STRING",
            "description": (
                "Sunday-evening 'this week' digest body — third-person "
                "narrative, no salutations. Ships verbatim into the Gmail "
                "draft body. On a quiet week, explain what was reviewed "
                "and acknowledge that nothing was flagged."
            ),
        },
        "candidates": {
            "type": "ARRAY",
            "nullable": True,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "summary": {"type": "STRING", "nullable": True},
                    "source_kind": {
                        "type": "STRING",
                        "description": (
                            "One of: triaged_item | routed_event | note | "
                            "decision | reflection. Must match the table the "
                            "win was extracted from."
                        ),
                    },
                    "source_id": {
                        "type": "STRING",
                        "description": (
                            "Originating row's primary key (item_id / "
                            "note_id / decision_id / reflection_id / "
                            "routed_events item_id+channel concatenation)."
                        ),
                    },
                    "evidence_links": {
                        "type": "ARRAY",
                        "nullable": True,
                        "items": {"type": "STRING"},
                    },
                },
                "required": ["title", "source_kind", "source_id"],
            },
        },
    },
    "required": ["commentary"],
}


class ParseError(ValueError):
    """LLM output didn't satisfy the schema after parsing."""


class StructuredLLMClient(Protocol):
    """Minimal LLM surface — returns raw JSON text."""

    def generate(self, *, prompt: str, model: str) -> str: ...


@dataclass(frozen=True)
class BragSpotterComposeConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_output_tokens: int = 8192


class VertexBragSpotterComposeClient:
    """Real `StructuredLLMClient` impl via `google.genai`."""

    def __init__(
        self,
        config: BragSpotterComposeConfig,
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

    def _get_or_build_client(self) -> Any:
        if self._client is None:
            from google import genai

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
            "response_schema": BRAG_SPOTTER_RESPONSE_SCHEMA,
        }
        thinking_config = getattr(genai_types, "ThinkingConfig", None)
        if thinking_config is not None:
            kwargs["thinking_config"] = thinking_config(thinking_budget=0)
        return genai_types.GenerateContentConfig(**kwargs)


# ---------------------------------------------------------------- composer


class BragSpotterComposer:
    """Renders prompt + invokes LLM + parses payload."""

    def __init__(
        self,
        *,
        prompt_template: str,
        llm: StructuredLLMClient,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._template = prompt_template
        self._llm = llm
        self._model = model

    def compose(
        self,
        *,
        recipient_email: str,
        week_of: date,
        triaged_items: Iterable[TriagedItemRow],
        routed_events: Iterable[RoutedEventRow],
        notes: Iterable[NoteRow],
        decisions: Iterable[DecisionRow],
        reflections: Iterable[ReflectionRow],
        existing_wins: Iterable[ExistingWinRow],
    ) -> BragSpotterPayload:
        blocks = render_section_blocks(
            triaged_items=list(triaged_items),
            routed_events=list(routed_events),
            notes=list(notes),
            decisions=list(decisions),
            reflections=list(reflections),
            existing_wins=list(existing_wins),
        )
        substitutions = {
            "recipient_email": recipient_email,
            "week_of_human": week_of.strftime("%B %d, %Y"),
            **blocks,
        }
        prompt = self._template
        for key, value in substitutions.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
        raw = self._llm.generate(prompt=prompt, model=self._model)
        return parse_payload(raw)


# ---------------------------------------------------------------- parsing


def parse_payload(raw_json: str) -> BragSpotterPayload:
    """Parse an LLM raw JSON string into a `BragSpotterPayload`.

    Drops candidates whose `source_kind` isn't on the allowlist (the
    LLM occasionally hallucinates new enum values; better to drop than
    accept). Required fields per candidate: `title`, `source_kind`,
    `source_id`. Missing → candidate dropped (logged), not raised.
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

    raw_candidates = data.get("candidates") or []
    if not isinstance(raw_candidates, list):
        raise ParseError("'candidates' must be a list")

    parsed: list[WinCandidate] = []
    for c in raw_candidates:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or "").strip()
        source_kind = str(c.get("source_kind") or "").strip()
        source_id = str(c.get("source_id") or "").strip()
        if not title or not source_kind or not source_id:
            log.info(
                "brag_spotter.compose.candidate_dropped reason=missing_field title=%r kind=%r",
                title,
                source_kind,
            )
            continue
        if source_kind not in VALID_SOURCE_KINDS:
            log.info(
                "brag_spotter.compose.candidate_dropped reason=invalid_source_kind kind=%r",
                source_kind,
            )
            continue
        evidence = c.get("evidence_links") or []
        if not isinstance(evidence, list):
            evidence = []
        evidence_tuple = tuple(str(e) for e in evidence if isinstance(e, str))
        parsed.append(
            WinCandidate(
                title=title,
                summary=_optional_str(c.get("summary")),
                source_kind=source_kind,
                source_id=source_id,
                evidence_links=evidence_tuple,
            )
        )

    return BragSpotterPayload(
        commentary=commentary.strip(),
        candidates=tuple(parsed),
    )


def _optional_str(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _extract_text(response: Any) -> str:
    """Mirror `evening_reflection.structured_compose._extract_text`."""
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


# ---------------------------------------------------------------- block render


def render_section_blocks(
    *,
    triaged_items: list[TriagedItemRow],
    routed_events: list[RoutedEventRow],
    notes: list[NoteRow],
    decisions: list[DecisionRow],
    reflections: list[ReflectionRow],
    existing_wins: list[ExistingWinRow],
) -> dict[str, str]:
    """Render each input list into a textual block for the prompt."""
    return {
        "triaged_items_block": _render_triaged_items(triaged_items),
        "routed_events_block": _render_routed_events(routed_events),
        "notes_block": _render_notes(notes),
        "decisions_block": _render_decisions(decisions),
        "reflections_block": _render_reflections(reflections),
        "existing_wins_block": _render_existing_wins(existing_wins),
    }


def _render_triaged_items(items: list[TriagedItemRow]) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        url = f" — {it.source_url}" if it.source_url else ""
        goal = f" [goal: {it.positive_goal_achieving}]" if it.positive_goal_achieving else ""
        lines.append(f"- id={it.item_id} [{it.severity}] ({it.source}) {it.reasoning}{goal}{url}")
    return "\n".join(lines)


def _render_routed_events(events: list[RoutedEventRow]) -> str:
    if not events:
        return "(none)"
    lines = []
    for e in events:
        lines.append(f"- id={e.item_id} channel={e.channel} routed_at={e.routed_at.isoformat()}")
    return "\n".join(lines)


def _render_notes(notes: list[NoteRow]) -> str:
    if not notes:
        return "(none)"
    lines = []
    for n in notes:
        snippet = (n.markdown_content or "").strip().replace("\n", " ")[:240]
        line = f"- id={n.note_id} ({n.extraction_method}) {n.filename}: {snippet}"
        if n.source_drive_url:
            line += f" — {n.source_drive_url}"
        lines.append(line)
    return "\n".join(lines)


def _render_decisions(decisions: list[DecisionRow]) -> str:
    if not decisions:
        return "(none)"
    lines = []
    for d in decisions:
        ctx = (d.context or "").strip().replace("\n", " ")[:160]
        lines.append(
            f"- id={d.decision_id} [{d.status}] {d.title} → {d.choice}"
            + (f" / context: {ctx}" if ctx else "")
        )
    return "\n".join(lines)


def _render_reflections(reflections: list[ReflectionRow]) -> str:
    if not reflections:
        return "(none)"
    lines = []
    for r in reflections:
        body = r.body_markdown.strip().replace("\n", " ")[:300]
        lines.append(f"- id={r.reflection_id} {r.local_date.isoformat()}: {body}")
    return "\n".join(lines)


def _render_existing_wins(wins: list[ExistingWinRow]) -> str:
    if not wins:
        return "(none)"
    lines = []
    for w in wins:
        lines.append(f"- {w.title} (kind={w.source_kind})")
    return "\n".join(lines)
