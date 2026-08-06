"""Vertex SDK extractor for entity-attribute facts (ADR 0070).

Mirrors ``commitment_extractor/extractor.py`` — ``google.genai.Client``
with ``vertexai=True`` and a strict ``response_schema``. One Flash call per
source note; ``thinking_budget=0`` keeps cost at pennies/month.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .models import ExtractedFact, SourceNote

log = logging.getLogger("agency_brain.agents.fact_extractor.extractor")

DEFAULT_FLASH_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_OUTPUT_TOKENS = 4096

_PRICE_PER_M_TOKENS: dict[str, dict[str, float]] = {
    DEFAULT_FLASH_MODEL: {"input": 0.30, "output": 2.50},
}

# Canonical predicate keys the model is asked to reuse (ADR 0070 §3). Free-form
# keys are allowed but fragment the same-key supersession rule.
CANONICAL_PREDICATES = (
    "retainer",
    "status",
    "role",
    "renewal_terms",
    "primary_contact",
    "billing",
    "scope",
    "channel_pref",
    "start_date",
    "end_date",
)

EXTRACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "facts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "entity_name": {"type": "STRING"},
                    "entity_email": {"type": "STRING", "nullable": True},
                    "predicate": {"type": "STRING"},
                    "value": {"type": "STRING"},
                    "observed_date": {"type": "STRING", "nullable": True},
                    "confidence": {
                        "type": "NUMBER",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "reasoning": {"type": "STRING"},
                },
                "required": [
                    "entity_name",
                    "predicate",
                    "value",
                    "confidence",
                    "reasoning",
                ],
            },
        },
    },
    "required": ["facts"],
}

_PROMPT_TEMPLATE = """\
You extract durable ENTITY-ATTRIBUTE FACTS from a single note in the operator's
agency "second brain". A fact is a stable attribute of a known entity — an
account/company or a person — that someone would want the current value of
later. Examples: "Acme's retainer is $3k/mo", "Tim is now the owner (not the
PM)", "WeCare project is paused", "renewal is annual in March".

NOT facts: one-off events, to-dos/promises (those are commitments), opinions,
transient logistics, anything not an attribute of a specific named entity.

Prefer these canonical predicate keys when they fit (reuse exact spelling):
{predicates}
If none fit, use a short snake_case key of your own.

The note's source kind is: {note_kind}
The note's date (best-effort) is: {note_date}
Today's date (UTC) is: {today}

For each fact return:
- entity_name: the company or person the fact is about (verbatim as written).
- entity_email: that person's email if present, else null.
- predicate: the attribute key (canonical when possible).
- value: the attribute's value, concise.
- observed_date: ISO date (YYYY-MM-DD) when the fact became true if stated or
  clearly implied (resolve relative dates against the note date); else null.
- confidence: 0.0-1.0 that this is a real, durable attribute (not noise).
- reasoning: one short sentence.

Only extract facts you are confident are durable attributes. If none, return an
empty array. Do not invent facts.

--- NOTE CONTENT ---
{content}
--- END NOTE ---
"""


@dataclass(frozen=True)
class ExtractorConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_FLASH_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class ExtractorResult:
    facts: tuple[ExtractedFact, ...]
    cost_usd: float


class Extractor:
    def __init__(self, config: ExtractorConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client

    def extract(self, *, note: SourceNote, today: str) -> ExtractorResult:
        prompt = _PROMPT_TEMPLATE.format(
            predicates=", ".join(CANONICAL_PREDICATES),
            note_kind=note.note_kind,
            note_date=note.note_date or "unknown",
            today=today,
            content=note.markdown_content[:12000],
        )
        client = self._get_or_build_client()
        config = self._build_generation_config()
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        response = client.models.generate_content(
            model=self._config.model,
            contents=contents,
            config=config,
        )
        raw_json = _extract_text(response)
        usage = _extract_usage(response)
        cost = _compute_cost(model=self._config.model, usage=usage)
        facts = _parse(raw_json, note_id=note.note_id)
        return ExtractorResult(facts=facts, cost_usd=cost)

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
            "response_schema": EXTRACTION_RESPONSE_SCHEMA,
        }
        thinking_config = getattr(genai_types, "ThinkingConfig", None)
        if thinking_config is not None:
            kwargs["thinking_config"] = thinking_config(thinking_budget=0)
        return genai_types.GenerateContentConfig(**kwargs)


def _extract_text(response: Any) -> str:
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
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if isinstance(t, str) and t:
                parts_text.append(t)
    if parts_text:
        return "".join(parts_text)
    return json.dumps({"facts": []})


def _extract_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {"input": 0, "output": 0}
    return {
        "input": int(getattr(usage, "prompt_token_count", 0) or 0),
        "output": int(getattr(usage, "candidates_token_count", 0) or 0),
    }


def _compute_cost(*, model: str, usage: dict[str, int]) -> float:
    price = _PRICE_PER_M_TOKENS.get(model)
    if price is None:
        return 0.0
    return round(
        (usage["input"] / 1_000_000.0) * price["input"]
        + (usage["output"] / 1_000_000.0) * price["output"],
        6,
    )


def _parse(raw_json: str, *, note_id: str | None = None) -> tuple[ExtractedFact, ...]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        # Drop the facts but record which note failed so a truncated /
        # malformed Gemini response is traceable instead of vanishing silently.
        log.warning("fact_extractor.extractor.parse_failed note_id=%s", note_id)
        return ()
    out: list[ExtractedFact] = []
    for f in data.get("facts") or []:
        entity = str(f.get("entity_name") or "").strip()
        predicate = str(f.get("predicate") or "").strip().lower().replace(" ", "_")
        value = str(f.get("value") or "").strip()
        if not entity or not predicate or not value:
            continue
        out.append(
            ExtractedFact(
                entity_name=entity,
                entity_email=(f.get("entity_email") or None),
                predicate=predicate,
                value=value,
                observed_date=(f.get("observed_date") or None),
                confidence=float(f.get("confidence") or 0.0),
                reasoning=str(f.get("reasoning") or ""),
            )
        )
    return tuple(out)
