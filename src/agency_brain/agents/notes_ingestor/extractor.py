"""Multimodal Markdown extractor for the notes ingestor.

ADR 0031 §2 introduced this for Samsung Notes PDFs. ADR 0037 §5 extends
it to handle audio (.m4a, .mp3, .wav), Markdown (.md), and Google Docs
(via Drive's built-in ``text/markdown`` export).

Single-model pipeline: Gemini 2.5 Flash multimodal accepts arbitrary
bytes via ``Part.from_data(data, mime_type)``. One LLM handles four
input shapes; the prompt is dispatched per MIME type. Markdown and
Google Doc inputs skip the LLM call entirely (passthrough).

The extractor is decoupled from the Vertex SDK by a ``MultimodalLLM``
Protocol so unit tests can pass a fake.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .models import ExtractionMethod, ExtractionResult

log = logging.getLogger("agency_brain.agents.notes_ingestor.extractor")

DEFAULT_MODEL = "gemini-2.5-flash"

# MIME types this extractor knows about. Anything else returns FAILED.
PDF_MIME_TYPE = "application/pdf"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
MARKDOWN_MIME_TYPES = {"text/markdown", "text/x-markdown"}
AUDIO_MIME_TYPES = {
    "audio/mp4",  # iOS .m4a
    "audio/x-m4a",  # alt
    "audio/mpeg",  # .mp3
    "audio/wav",
    "audio/x-wav",
}


# ---------------------------------------------------------------------------
# Prompts — one per multimodal input type
# ---------------------------------------------------------------------------

PDF_PROMPT = """You are an expert document extractor. The PDF is a single Samsung Notes \
note exported by the user. Convert the entire note to clean Markdown.

REQUIREMENTS:
- Preserve hierarchy: use `#`/`##`/`###` for headings, `-` for bullets, \
`1.` for numbered lists, `**bold**`, `*italic*`, fenced code blocks for \
code/CLI snippets.
- Convert handwriting to text. If a passage is illegible, write \
`[illegible]` rather than guess.
- For each image, sticker, screenshot, or hand-drawn diagram, emit a \
single line of the form `[Image: <one-sentence factual description>]` \
inline at the position the visual appears. Capture annotations \
(arrows, circles, labels) in the description.
- Do not add commentary, do not summarize, do not invent content. \
Output only the Markdown body.
- Output a final line on its own:
  `<<<EXTRACTION_META>>>{"page_count": <int>, "confidence": <float 0..1>, \
"notes": "<short diagnostic or empty string>"}`

The `confidence` field is your self-rated overall extraction quality on \
[0,1]: 1.0 = clean text, 0.7 = partial OCR/some illegible regions, \
0.3 = mostly illegible. The `notes` field is a brief diagnostic if \
anything was lost (e.g., "page 3 illegible") — empty string when \
clean."""

# Back-compat name — preserved as the export tests pin.
EXTRACTION_PROMPT = PDF_PROMPT

AUDIO_PROMPT = """You are an expert audio transcriber. The audio is a personal \
voice memo recorded by the user. Convert the spoken content to clean \
Markdown.

REQUIREMENTS:
- Transcribe verbatim. Preserve fillers ("um", "uh") only if they \
signal hesitation; otherwise drop them.
- Use `## ` headings to mark topic shifts when the speaker pauses \
meaningfully.
- Use bullet points when the speaker enumerates.
- Annotate timestamps every ~30 seconds in the form `[mm:ss]` at the \
start of a paragraph.
- If a passage is inaudible, write `[inaudible]` rather than guess.
- Do not add commentary, do not summarize, do not invent content. \
Output only the Markdown transcript.
- Output a final line on its own:
  `<<<EXTRACTION_META>>>{"page_count": 0, "confidence": <float 0..1>, \
"notes": "<short diagnostic or empty string>"}`

`page_count` is always 0 for audio. The `confidence` field: 1.0 = \
clean transcription, 0.7 = some inaudible regions, 0.3 = mostly \
inaudible."""


_META_MARKER = "<<<EXTRACTION_META>>>"


class ExtractionError(RuntimeError):
    """Raised when the LLM returns a response we can't interpret."""


class MultimodalLLM(Protocol):
    """Generates content from a (text prompt, raw bytes, mime_type) tuple.

    Signature changed in ADR 0037 §5: the original Protocol took
    ``pdf_bytes``; the generalized Protocol accepts arbitrary bytes
    plus a ``mime_type`` so audio/doc/PDF can share the wrapper.
    """

    def generate(self, *, prompt: str, data: bytes, mime_type: str, model: str) -> str: ...


class GeminiMultimodalExtractor:
    """Multimodal Markdown extractor — picks a prompt by MIME type.

    Dispatch table:

    | Input MIME type                      | Path           | extraction_method          |
    |--------------------------------------|----------------|----------------------------|
    | application/pdf                      | LLM (PDF prompt) | gemini-2.5-flash         |
    | audio/mp4 / audio/mpeg / audio/wav   | LLM (audio prompt) | gemini-2.5-flash-audio |
    | text/markdown                        | passthrough    | markdown-passthrough       |
    | application/vnd.google-apps.document | passthrough*   | gemini-2.5-flash-doc-export |
    | (anything else)                      | failed         | failed                     |

    *The Drive client exports Google Docs to ``text/markdown`` at
    download time, so by the time bytes reach the extractor the content
    is already MD. The distinct ``extraction_method`` value records
    that the source was a Doc, useful for downstream diagnostics.
    """

    def __init__(self, *, llm: MultimodalLLM, model: str = DEFAULT_MODEL) -> None:
        self._llm = llm
        self._model = model

    def extract(self, *, data: bytes, mime_type: str, file_name: str = "") -> ExtractionResult:
        if mime_type == PDF_MIME_TYPE:
            return self._extract_via_llm(
                data=data,
                mime_type=mime_type,
                prompt=PDF_PROMPT,
                method=ExtractionMethod.GEMINI_FLASH,
            )
        if mime_type in AUDIO_MIME_TYPES:
            return self._extract_via_llm(
                data=data,
                mime_type=mime_type,
                prompt=AUDIO_PROMPT,
                method=ExtractionMethod.GEMINI_FLASH_AUDIO,
            )
        if mime_type in MARKDOWN_MIME_TYPES:
            return _passthrough(data=data, method=ExtractionMethod.MARKDOWN_PASSTHROUGH)
        if mime_type == GOOGLE_DOC_MIME_TYPE:
            # By the time bytes reach here the Drive client has already
            # exported the doc to text/markdown — Gemini didn't run.
            # The method tag preserves the Doc provenance.
            return _passthrough(data=data, method=ExtractionMethod.GEMINI_FLASH_DOC_EXPORT)

        log.warning(
            "notes_ingestor.extract.unsupported_mime mime_type=%s file=%s",
            mime_type,
            file_name,
        )
        return ExtractionResult(
            markdown="",
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes=f"unsupported MIME type: {mime_type}",
        )

    def _extract_via_llm(
        self,
        *,
        data: bytes,
        mime_type: str,
        prompt: str,
        method: ExtractionMethod,
    ) -> ExtractionResult:
        try:
            raw = self._llm.generate(
                prompt=prompt,
                data=data,
                mime_type=mime_type,
                model=self._model,
            )
        except Exception as exc:
            log.exception("notes_ingestor.extract.llm_failed")
            return ExtractionResult(
                markdown="",
                page_count=0,
                method=ExtractionMethod.FAILED,
                confidence=0.0,
                notes=f"LLM call failed: {type(exc).__name__}: {exc}",
            )
        return _parse_response(raw, method=method)


# Back-compat shim for ADR 0031 callers (PDF only). New code should use
# ``GeminiMultimodalExtractor`` directly.
class GeminiPDFExtractor:
    """Extracts Markdown + meta from a PDF via a multimodal LLM.

    Preserved for ADR 0031 callers. Internally delegates to
    ``GeminiMultimodalExtractor`` with the PDF MIME type.
    """

    def __init__(self, *, llm: MultimodalLLM, model: str = DEFAULT_MODEL) -> None:
        self._inner = GeminiMultimodalExtractor(llm=llm, model=model)

    def extract(self, *, pdf_bytes: bytes) -> ExtractionResult:
        return self._inner.extract(
            data=pdf_bytes,
            mime_type=PDF_MIME_TYPE,
            file_name="",
        )


def _passthrough(*, data: bytes, method: ExtractionMethod) -> ExtractionResult:
    """No-LLM extraction path for already-clean Markdown bytes."""
    try:
        markdown = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ExtractionResult(
            markdown="",
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes=f"could not decode markdown bytes: {exc}",
        )
    if not markdown.strip():
        return ExtractionResult(
            markdown="",
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes="empty markdown body",
        )
    return ExtractionResult(
        markdown=markdown.strip(),
        page_count=0,
        method=method,
        confidence=1.0,
        notes=None,
    )


def _parse_response(
    raw: str, *, method: ExtractionMethod | str = ExtractionMethod.GEMINI_FLASH
) -> ExtractionResult:
    """Split the LLM response into markdown body + JSON meta tail.

    The model is instructed to put the meta on its own final line. We
    parse from the back; everything before the meta line is the
    Markdown body. If the meta line is missing or unparseable we still
    return the body but with FAILED method + confidence=0 — the row
    lands so the corpus stays consistent, but downstream RAG can filter
    on `extraction_method`.

    The ``method`` parameter is normalized:
    - An ``ExtractionMethod`` enum is used as-is.
    - A bare string (model name) is resolved to ``GEMINI_FLASH`` for
      back-compat with ADR 0031 callers that passed the model id here.
    """
    if isinstance(method, str):
        if method in ExtractionMethod._value2member_map_:
            resolved = ExtractionMethod(method)
        else:
            resolved = ExtractionMethod.GEMINI_FLASH
    else:
        resolved = method

    text = (raw or "").strip()
    if not text:
        return ExtractionResult(
            markdown="",
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes="empty response",
        )

    idx = text.rfind(_META_MARKER)
    if idx == -1:
        log.warning("notes_ingestor.extract.no_meta_line")
        return ExtractionResult(
            markdown=text,
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes="response missing EXTRACTION_META line",
        )

    body = text[:idx].rstrip()
    meta_raw = text[idx + len(_META_MARKER) :].strip()
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError as exc:
        log.warning("notes_ingestor.extract.bad_meta_json: %s", exc)
        return ExtractionResult(
            markdown=body,
            page_count=0,
            method=ExtractionMethod.FAILED,
            confidence=0.0,
            notes=f"bad EXTRACTION_META json: {exc}",
        )

    return ExtractionResult(
        markdown=body,
        page_count=int(meta.get("page_count", 0)),
        method=resolved,
        confidence=_clamp_confidence(meta.get("confidence", 0.0)),
        notes=(meta.get("notes") or None) or None,
    )


def _clamp_confidence(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


class VertexMultimodalLLM:
    """Production ``MultimodalLLM`` wired to Vertex via ``google-genai``.

    Migrated from the deprecated ``vertexai`` SDK to ``google.genai.Client(vertexai=True)``
    on 2026-05-28 per audit F4 Phase 1 (deprecated SDK EOL 2026-06-24). Lazy-imports
    the SDK so unit tests can swap a fake without the runtime dep.
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

    def generate(self, *, prompt: str, data: bytes, mime_type: str, model: str) -> str:
        client = self._ensure_client()
        from google.genai import types as genai_types

        # ``Part.from_bytes`` accepts arbitrary bytes + mime_type; the model
        # then sees the binary blob as a multimodal input alongside the
        # text prompt. Same call shape for PDF, audio, and (future) image.
        response = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=data, mime_type=mime_type),
                prompt,
            ],
        )
        return response.text or ""
