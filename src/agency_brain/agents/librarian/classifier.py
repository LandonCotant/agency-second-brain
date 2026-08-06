"""Gemini 2.5 Flash classifier for the Librarian (ADR 0044, Phase D).

Pure function-of-content: the classifier sees the file's extracted
markdown + the current Areas-folder candidate list, and emits a single
``LibrarianClassification`` (dest_folder_path | None, confidence,
reasoning).

Mirrors ``notes_ingestor.extractor.GeminiMultimodalExtractor`` ergonomics:
the Vertex SDK is imported lazily; the ``StructuredLLMClient`` Protocol
keeps tests fast (a fake returns a canned JSON string).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .models import AreaFolder, LibrarianClassification

log = logging.getLogger("agency_brain.agents.librarian.classifier")


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "dest_folder_path": {
            "type": "STRING",
            "nullable": True,
            "description": (
                "Path of the candidate folder that best fits this file "
                "(verbatim from the candidates list). NULL when no "
                "candidate is a confident match."
            ),
        },
        "confidence": {
            "type": "NUMBER",
            "description": "Self-rated confidence in [0, 1].",
        },
        "reasoning": {
            "type": "STRING",
            "description": "1-2 sentences explaining the choice.",
        },
        "suggested_description": {
            "type": "STRING",
            "nullable": True,
            "description": (
                "Short descriptor (<=6 words, lowercase, hyphen-separated) "
                "used by the renamer when the original filename is auto-"
                "generated. Examples: 'site-copy-review', 'q3-budget-draft'. "
                "Omit punctuation and stop words. NULL when the file's "
                "content is too generic to summarize."
            ),
        },
    },
    "required": ["dest_folder_path", "confidence", "reasoning"],
}


class StructuredLLMClient(Protocol):
    def generate(self, *, prompt: str, model: str) -> str: ...


class ClassifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class LibrarianClassifyConfig:
    project_id: str
    location: str = DEFAULT_LOCATION
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_output_tokens: int = 1024
    max_content_chars: int = 2000
    """Cap on how much of the file's extracted markdown we feed the LLM —
    classification doesn't need the whole 20-page PDF, the first ~2k
    chars are plenty."""


class LibrarianClassifier:
    """Wraps a structured LLM call returning a ``LibrarianClassification``."""

    def __init__(self, *, llm: StructuredLLMClient, config: LibrarianClassifyConfig) -> None:
        self._llm = llm
        self._config = config

    def classify(
        self,
        *,
        filename: str,
        extracted_markdown: str,
        candidates: list[AreaFolder],
    ) -> LibrarianClassification:
        """Send the file content + candidate list, parse the JSON response."""
        if not candidates:
            # Empty Areas/ tree — short-circuit to None confidence so the
            # caller drops to _uncategorized/ without paying for an LLM call.
            return LibrarianClassification(
                dest_folder_path=None,
                confidence=0.0,
                reasoning="No Areas folders configured yet — falling back to _uncategorized/.",
            )

        prompt = _build_prompt(
            filename=filename,
            content=extracted_markdown[: self._config.max_content_chars],
            candidates=candidates,
        )
        try:
            raw = self._llm.generate(prompt=prompt, model=self._config.model)
        except Exception as exc:
            raise ClassifyError(f"Vertex generate raised {type(exc).__name__}: {exc}") from exc
        return _parse_classification(raw)


def _build_prompt(*, filename: str, content: str, candidates: list[AreaFolder]) -> str:
    candidate_lines = "\n".join(f"- {f.path}" for f in candidates)
    return f"""You are the Agency Second Brain Librarian. Classify a single file dropped into
the unsorted inbox under the right Areas/ folder so future agents can
find it via topic + semantic search.

Output a JSON object exactly matching the response schema:
- dest_folder_path: one of the candidate paths below (string), OR null
  when no candidate is a confident match.
- confidence: your self-rated probability in [0, 1] that the chosen
  path is correct. Use 0.0 with a null path when nothing fits.
- reasoning: 1-2 sentences explaining the choice in concrete terms
  (e.g. "names Client A repeatedly", "describes the local-service
  onboarding playbook").

Hard rules:
- Pick from the candidate list verbatim. Do NOT invent paths.
- If the content is too short / generic to classify confidently, return
  null with confidence below 0.5. Better the file lands in
  _uncategorized/ than wrong.
- Don't overweight filename: a memo named "notes.md" is still a memo.
- For client material, prefer the most specific path (e.g.
  ``clients/clienta-pi`` over ``clients``).

Areas vs Resources (when both are candidates, ADR 0054):
- An Area is an ongoing responsibility OR the work itself — a meeting
  notes file, a client strategy doc, a personal Reflection, a per-area
  log. Areas paths typically live under ``brain/...`` or ``clients/...``.
- A Resource is a template, framework, factual reference, or prompt —
  something you reach for *when doing work*, reusable across projects
  (e.g. a meeting-agenda template, a discovery-call prompt, a copywriting
  framework). Resources paths typically live under ``resources/...``.
- When in doubt, prefer Areas. Resources is reserved for content
  obviously reusable across multiple projects/clients.

Filename: {filename}

Content (truncated):
{content or '(empty)'}

Candidate folders (Areas-relative paths):
{candidate_lines or '(none)'}
"""


def _parse_classification(raw: str) -> LibrarianClassification:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ClassifyError(f"LLM output was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ClassifyError(f"LLM output was not an object: {type(data).__name__}")
    dest = data.get("dest_folder_path")
    dest_str = str(dest).strip() if isinstance(dest, str) and dest.strip() else None
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(data.get("reasoning") or "").strip() or "(no reasoning provided)"
    suggested = data.get("suggested_description")
    suggested_clean = (
        str(suggested).strip() if isinstance(suggested, str) and suggested.strip() else None
    )
    return LibrarianClassification(
        dest_folder_path=dest_str,
        confidence=confidence,
        reasoning=reasoning,
        suggested_description=suggested_clean,
    )


# --------------------------------------------------------------- production


class VertexLibrarianClassifier:
    """Production ``StructuredLLMClient`` backed by ``google.genai``.

    Mirrors ``evening_reflection.structured_compose.VertexStructuredComposeClient``.
    Lazily builds the SDK client so unit tests + cold start stay cheap.
    """

    def __init__(self, config: LibrarianClassifyConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client

    def generate(self, *, prompt: str, model: str | None = None) -> str:
        client = self._get_or_build_client()
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        cfg = self._build_generation_config()
        response = client.models.generate_content(
            model=model or self._config.model,
            contents=contents,
            config=cfg,
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
            "response_schema": _RESPONSE_SCHEMA,
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
    return "".join(parts_text) or json.dumps(
        {"_extract_failed": True, "_repr": repr(response)[:500]}
    )
