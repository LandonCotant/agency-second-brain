"""Vertex SDK extractor for commitments (ADR 0069).

Mirrors ``crm_updater/extractor.py`` — ``google.genai.Client`` with
``vertexai=True`` and a strict ``response_schema``. One Flash call per
source note; ``thinking_budget=0`` keeps cost at pennies/month.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .models import VALID_DIRECTIONS, ExtractedCommitment, SourceNote

log = logging.getLogger("agency_brain.agents.commitment_extractor.extractor")

DEFAULT_FLASH_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_OUTPUT_TOKENS = 4096

_PRICE_PER_M_TOKENS: dict[str, dict[str, float]] = {
    DEFAULT_FLASH_MODEL: {"input": 0.30, "output": 2.50},
}

EXTRACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "commitments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "direction": {"type": "STRING", "enum": list(VALID_DIRECTIONS)},
                    "commitment_text": {"type": "STRING"},
                    "counterparty_email": {"type": "STRING", "nullable": True},
                    "counterparty_name": {"type": "STRING", "nullable": True},
                    "due_date": {"type": "STRING", "nullable": True},
                    "confidence": {
                        "type": "NUMBER",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "reasoning": {"type": "STRING"},
                },
                "required": [
                    "direction",
                    "commitment_text",
                    "confidence",
                    "reasoning",
                ],
            },
        },
    },
    "required": ["commitments"],
}

_PROMPT_TEMPLATE = """\
You extract COMMITMENTS from a single note in the operator's "second brain" corpus.
A commitment is a concrete promise to do something — by the operator ("mine") or by
someone else to the operator ("theirs"). Examples: "I'll send the proposal Friday",
"Tim will get us the signed docs next week", "let me circle back with numbers".

NOT commitments: vague intentions ("we should chat sometime"), completed past
actions, questions, FYIs, calendar logistics with no promise, generic pleasantries.

The note's source kind is: {note_kind}
Today's date (UTC) is: {today}

For each genuine commitment, return:
- direction: "mine" if the operator is the one who promised; "theirs" if someone
  promised the operator.
- commitment_text: a concise paraphrase of what was promised.
- counterparty_email / counterparty_name: the OTHER party (best-effort; null if
  unknown). For "mine", the recipient; for "theirs", the promisor.
- due_date: an ISO date (YYYY-MM-DD) if a deadline is stated or clearly implied
  (resolve relative dates like "Friday"/"next week" against today's date); else null.
- confidence: 0.0-1.0 that this is a real, actionable commitment (not noise).
- reasoning: one short sentence on why this is a commitment.

If there are no commitments, return an empty array. Do not invent commitments.

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
    commitments: tuple[ExtractedCommitment, ...]
    cost_usd: float


class Extractor:
    def __init__(self, config: ExtractorConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client

    def extract(self, *, note: SourceNote, today: str) -> ExtractorResult:
        prompt = _PROMPT_TEMPLATE.format(
            note_kind=note.note_kind,
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
        commitments = _parse(raw_json, note_id=note.note_id)
        return ExtractorResult(commitments=commitments, cost_usd=cost)

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
    return json.dumps({"commitments": []})


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


def _parse(raw_json: str, *, note_id: str | None = None) -> tuple[ExtractedCommitment, ...]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        # Drop the commitment but record which note failed so a truncated /
        # malformed Gemini response is traceable instead of vanishing silently.
        log.warning("commitment_extractor.extractor.parse_failed note_id=%s", note_id)
        return ()
    out: list[ExtractedCommitment] = []
    for c in data.get("commitments") or []:
        text = str(c.get("commitment_text") or "").strip()
        direction = str(c.get("direction") or "").strip()
        if not text or direction not in VALID_DIRECTIONS:
            continue
        out.append(
            ExtractedCommitment(
                direction=direction,
                commitment_text=text,
                counterparty_email=(c.get("counterparty_email") or None),
                counterparty_name=(c.get("counterparty_name") or None),
                due_date=(c.get("due_date") or None),
                confidence=float(c.get("confidence") or 0.0),
                reasoning=str(c.get("reasoning") or ""),
            )
        )
    return tuple(out)
