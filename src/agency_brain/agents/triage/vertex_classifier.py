"""Real Vertex/Gemini implementation of `TriageClassifierClient`.

Uses the modern `google.genai` SDK. Each `classify()` call:
1. Composes the full prompt (template + signal block).
2. Calls `models.generate_content` with `model_armor_config` referencing our
   Model Armor Template (provisioned in PR 4b) and `response_schema` for
   controlled JSON generation.
3. Returns the raw text the model produced.

Model Armor is enforced **at the API call**, not as a config block on the
Reasoning Engine itself (PRD §4.4 / ADR 0015).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"
TRIAGE_TEMPLATE_NAME_FMT = "projects/{project}/locations/{location}/templates/asb-agent-triage"

# Schema for controlled generation. Vertex's `response_schema` is the OpenAPI
# Schema Object subset — uppercase types, `nullable: true` rather than
# JSON-Schema's `type: ["string", "null"]`, no `null` literals inside `enum`.
# Keep aligned with TriageOutput.__post_init__ invariants — the model must
# produce a JSON shape these dataclasses can parse.
TRIAGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "actionable": {"type": "BOOLEAN"},
        "positive_goal_achieving": {
            "type": "STRING",
            "nullable": True,
            "enum": ["strong", "moderate", "weak"],
        },
        "owner_type": {"type": "STRING", "enum": ["brian", "delegate", "na"]},
        "owner_email": {"type": "STRING", "nullable": True},
        "action_type": {
            "type": "STRING",
            "enum": ["do_now", "delegate", "defer", "schedule", "wait"],
        },
        "category": {
            "type": "STRING",
            "nullable": True,
            "enum": [
                "calls",
                "computer",
                "errands",
                "office",
                "schedule",
                "team_meeting",
                "staff",
                "waiting_for",
                "home",
            ],
        },
        "task_or_project": {
            "type": "STRING",
            "nullable": True,
            "enum": ["task", "project"],
        },
        "severity": {
            "type": "STRING",
            "enum": ["critical", "high", "medium", "low", "info"],
        },
        "confidence": {"type": "NUMBER", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "STRING"},
    },
    "required": [
        "actionable",
        "positive_goal_achieving",
        "owner_type",
        "action_type",
        "severity",
        "confidence",
        "reasoning",
    ],
}
# positive_goal_achieving is `nullable: true` (legal as null when actionable=false)
# but listed in `required` so the model must make a deliberate choice rather than
# omitting the field. The TriageOutput dataclass enforces the conditional
# (PGA != null when actionable=true) — without `required` here, gemini-2.5-flash
# silently drops the field on the actionable=true path and dataclass parsing fails.


@dataclass(frozen=True)
class VertexClassifierConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    temperature: float = 0.2  # low: classification, not creative writing
    # Gemini 2.5 models can spend output budget on hidden thinking tokens.
    # Keep this comfortably above the tiny JSON payload so schema-constrained
    # responses do not get truncated mid-string in production.
    max_output_tokens: int = 8192
    enable_model_armor: bool = True  # PRD §4.4 default; v0 deploy disables until IAM debugged

    @property
    def model_armor_template_name(self) -> str:
        return TRIAGE_TEMPLATE_NAME_FMT.format(project=self.project_id, location=self.location)


class VertexClassifier:
    """Real `TriageClassifierClient` impl backed by google.genai.

    The Vertex client is constructed lazily on first call so unit tests can
    stub it out without paying the import cost or needing GCP credentials.
    """

    def __init__(
        self,
        config: VertexClassifierConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client  # If None, lazily built on first classify()

    def classify(self, *, prompt: str, signal_block: str) -> str:
        """Call generate_content; return the raw response text.

        The text is JSON (controlled generation via response_schema). The
        TriageAgent is responsible for parsing it.
        """
        client = self._get_or_build_client()
        config = self._build_generation_config()
        contents = self._build_contents(prompt=prompt, signal_block=signal_block)
        response = client.models.generate_content(
            model=self._config.model,
            contents=contents,
            config=config,
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
        from google.genai import types as genai_types  # lazy import

        kwargs: dict[str, Any] = {
            "temperature": self._config.temperature,
            "max_output_tokens": self._config.max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": TRIAGE_RESPONSE_SCHEMA,
        }
        thinking_config = getattr(genai_types, "ThinkingConfig", None)
        if thinking_config is not None:
            kwargs["thinking_config"] = thinking_config(thinking_budget=0)
        if self._config.enable_model_armor:
            kwargs["model_armor_config"] = genai_types.ModelArmorConfig(
                prompt_template_name=self._config.model_armor_template_name,
                response_template_name=self._config.model_armor_template_name,
            )
        return genai_types.GenerateContentConfig(**kwargs)

    def _build_contents(self, *, prompt: str, signal_block: str) -> list[Any]:
        # Prompt template carries the system-level instructions + the signal.
        # signal_block is sent as a structured JSON addendum — the model can
        # reference both. Single user-role turn keeps things simple.
        body = prompt + "\n\n--- structured signal ---\n" + signal_block
        return [{"role": "user", "parts": [{"text": body}]}]


def _extract_text(response: Any) -> str:
    """Pull the text out of a generate_content response. The exact shape
    depends on the SDK version, so we handle a few plausible layouts."""
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

    # Last-ditch: stringify the response. Triage will fail to parse; the
    # error path emits a useful failure audit row.
    return json.dumps({"_extract_failed": True, "_repr": repr(response)[:500]})
