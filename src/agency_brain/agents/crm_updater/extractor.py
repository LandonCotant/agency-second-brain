"""Vertex SDK extractor for the CRM Auto-updater (ADR 0047).

Mirrors ``triage/vertex_classifier.py`` — ``google.genai.Client`` with
``vertexai=True`` and a strict ``response_schema``. One Flash call per
email; no Pro escalation in v1 (volume is low).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .models import (
    AccountMention,
    ContactUpdate,
    ExtractedTask,
    ExtractionResult,
    GmailMessage,
)
from .prompts import render_extraction_prompt

log = logging.getLogger("agency_brain.agents.crm_updater.extractor")

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
        "extracted_tasks": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "due_date": {"type": "STRING", "nullable": True},
                    "linked_account_name": {"type": "STRING", "nullable": True},
                    "linked_contact_email": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["title", "confidence"],
            },
        },
        "contact_updates": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "contact_email": {"type": "STRING"},
                    "last_contact_date": {"type": "STRING", "nullable": True},
                    "next_followup_suggested": {"type": "STRING", "nullable": True},
                    "warmth_change": {
                        "type": "STRING",
                        "enum": ["unchanged", "warmer", "cooler"],
                    },
                    "context_note": {"type": "STRING"},
                },
                "required": [
                    "contact_email",
                    "warmth_change",
                    "context_note",
                ],
            },
        },
        "account_mentions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "account_name": {"type": "STRING"},
                    "context_note": {"type": "STRING"},
                    "new_contacts": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                },
                "required": ["account_name", "context_note"],
            },
        },
    },
    "required": ["extracted_tasks", "contact_updates", "account_mentions"],
}


@dataclass(frozen=True)
class ExtractorConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_FLASH_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class ExtractorResult:
    extraction: ExtractionResult
    cost_usd: float
    confidence: float


class Extractor:
    def __init__(
        self,
        config: ExtractorConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client

    def extract(
        self,
        *,
        message: GmailMessage,
        known_account_names: tuple[str, ...] = (),
        known_contact_emails: tuple[str, ...] = (),
    ) -> ExtractorResult:
        prompt = render_extraction_prompt(
            message=message,
            known_account_names=known_account_names,
            known_contact_emails=known_contact_emails,
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
        extraction = _parse(raw_json)
        confidence = _aggregate_confidence(extraction)
        return ExtractorResult(extraction=extraction, cost_usd=cost, confidence=confidence)

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
    return json.dumps({"_extract_failed": True, "_repr": repr(response)[:500]})


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


def _parse(raw_json: str) -> ExtractionResult:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        log.warning("crm_updater.extractor.parse_failed")
        return ExtractionResult(
            extracted_tasks=(),
            contact_updates=(),
            account_mentions=(),
        )
    return ExtractionResult(
        extracted_tasks=tuple(
            ExtractedTask(
                title=str(t.get("title") or ""),
                due_date=t.get("due_date"),
                linked_account_name=t.get("linked_account_name"),
                linked_contact_email=t.get("linked_contact_email"),
                confidence=float(t.get("confidence") or 0.0),
            )
            for t in (data.get("extracted_tasks") or [])
            if t.get("title")
        ),
        contact_updates=tuple(
            ContactUpdate(
                contact_email=str(c.get("contact_email") or ""),
                last_contact_date=c.get("last_contact_date"),
                next_followup_suggested=c.get("next_followup_suggested"),
                warmth_change=str(c.get("warmth_change") or "unchanged"),
                context_note=str(c.get("context_note") or ""),
            )
            for c in (data.get("contact_updates") or [])
            if c.get("contact_email")
        ),
        account_mentions=tuple(
            AccountMention(
                account_name=str(a.get("account_name") or ""),
                context_note=str(a.get("context_note") or ""),
                new_contacts=tuple(str(e) for e in (a.get("new_contacts") or []) if e),
            )
            for a in (data.get("account_mentions") or [])
            if a.get("account_name")
        ),
    )


def _aggregate_confidence(extraction: ExtractionResult) -> float:
    """Mean task confidence (0.0 if no tasks). The contact / account
    arrays don't carry per-item confidence in v1; the BaseAgent's
    human-review-routed flag uses this aggregate."""
    if not extraction.extracted_tasks:
        return 0.0
    return sum(t.confidence for t in extraction.extracted_tasks) / len(extraction.extracted_tasks)
