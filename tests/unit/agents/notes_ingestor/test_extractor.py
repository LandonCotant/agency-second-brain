"""Unit tests for the notes ingestor's Vertex Gemini extractor.

Originally ADR 0031 (PDF-only). ADR 0037 §5 generalized the
``MultimodalLLM`` Protocol to take ``data`` + ``mime_type``; the
back-compat ``GeminiPDFExtractor`` shim still works for callers that
only handle PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agency_brain.agents.notes_ingestor.extractor import (
    AUDIO_PROMPT,
    EXTRACTION_PROMPT,
    GeminiMultimodalExtractor,
    GeminiPDFExtractor,
    _parse_response,
)
from agency_brain.agents.notes_ingestor.models import ExtractionMethod


@dataclass
class _FakeLLM:
    response: str = ""
    raises: BaseException | None = None
    captured: list[dict] = field(default_factory=list)

    def generate(self, *, prompt: str, data: bytes, mime_type: str, model: str) -> str:
        self.captured.append(
            {
                "prompt": prompt,
                "data_size": len(data),
                "mime_type": mime_type,
                "model": model,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.response


# ---------------------------------------------------------------------------
# PDF path — back-compat with ADR 0031
# ---------------------------------------------------------------------------


def test_extract_parses_clean_response():
    llm = _FakeLLM(
        response=(
            "# My note\n"
            "- bullet one\n"
            "- bullet two\n"
            '<<<EXTRACTION_META>>>{"page_count": 2, "confidence": 0.95, "notes": ""}'
        )
    )
    extractor = GeminiPDFExtractor(llm=llm, model="gemini-2.5-flash")

    result = extractor.extract(pdf_bytes=b"%PDF-fake-bytes")

    assert result.markdown.startswith("# My note")
    assert "bullet one" in result.markdown
    assert "<<<EXTRACTION_META>>>" not in result.markdown
    assert result.page_count == 2
    assert result.confidence == 0.95
    assert result.method == ExtractionMethod.GEMINI_FLASH
    assert result.notes is None
    # And the LLM was called with the right inputs.
    assert len(llm.captured) == 1
    assert llm.captured[0]["model"] == "gemini-2.5-flash"
    assert llm.captured[0]["data_size"] == len(b"%PDF-fake-bytes")
    assert llm.captured[0]["mime_type"] == "application/pdf"
    assert "Markdown" in llm.captured[0]["prompt"]


def test_extract_clamps_confidence_above_one():
    llm = _FakeLLM(
        response=("body\n" '<<<EXTRACTION_META>>>{"page_count": 1, "confidence": 1.7, "notes": ""}')
    )
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.confidence == 1.0


def test_extract_clamps_confidence_below_zero():
    llm = _FakeLLM(
        response=(
            "body\n" '<<<EXTRACTION_META>>>{"page_count": 1, "confidence": -0.5, "notes": ""}'
        )
    )
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.confidence == 0.0


def test_extract_returns_failed_on_llm_exception():
    llm = _FakeLLM(raises=RuntimeError("vertex went sideways"))
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.method == ExtractionMethod.FAILED
    assert result.confidence == 0.0
    assert result.markdown == ""
    assert result.notes is not None
    assert "vertex went sideways" in result.notes


def test_extract_returns_failed_on_empty_response():
    llm = _FakeLLM(response="")
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.method == ExtractionMethod.FAILED
    assert result.notes == "empty response"


def test_extract_marks_failed_when_meta_line_missing():
    """Body still survives — corpus row lands but downstream can filter."""
    llm = _FakeLLM(response="# Just markdown without a meta line\n- a")
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.method == ExtractionMethod.FAILED
    assert result.markdown.startswith("# Just markdown")
    assert result.notes is not None
    assert "missing EXTRACTION_META" in result.notes


def test_extract_marks_failed_when_meta_json_invalid():
    llm = _FakeLLM(response="body\n<<<EXTRACTION_META>>>{not json")
    extractor = GeminiPDFExtractor(llm=llm)

    result = extractor.extract(pdf_bytes=b"x")

    assert result.method == ExtractionMethod.FAILED
    assert result.markdown == "body"
    assert result.notes is not None
    assert "bad EXTRACTION_META json" in result.notes


def test_parse_response_strips_meta_line_only():
    """The meta line must not bleed into the markdown body."""
    raw = (
        "# heading\n"
        "para 1\n"
        '<<<EXTRACTION_META>>>{"page_count": 1, "confidence": 1.0, "notes": ""}'
    )

    result = _parse_response(raw, method=ExtractionMethod.GEMINI_FLASH)

    assert result.markdown.endswith("para 1")
    assert "EXTRACTION_META" not in result.markdown


def test_extraction_prompt_mentions_image_caption_format():
    """Sanity: the prompt is the contract with the model."""
    assert "[Image:" in EXTRACTION_PROMPT
    assert "Markdown" in EXTRACTION_PROMPT
    assert "EXTRACTION_META" in EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# ADR 0037 — multi-MIME via GeminiMultimodalExtractor
# ---------------------------------------------------------------------------


def test_multimodal_extracts_audio_via_audio_prompt():
    """Audio bytes route through the AUDIO_PROMPT, not the PDF prompt."""
    llm = _FakeLLM(
        response=(
            "## Topic shift\n"
            "[00:30] Some words.\n"
            '<<<EXTRACTION_META>>>{"page_count": 0, "confidence": 0.9, "notes": ""}'
        )
    )
    extractor = GeminiMultimodalExtractor(llm=llm, model="gemini-2.5-flash")

    result = extractor.extract(data=b"fake-m4a-bytes", mime_type="audio/mp4", file_name="memo.m4a")

    assert result.method == ExtractionMethod.GEMINI_FLASH_AUDIO
    assert result.page_count == 0
    assert result.confidence == 0.9
    assert "[00:30]" in result.markdown
    assert llm.captured[0]["mime_type"] == "audio/mp4"
    # Audio prompt — not the PDF one.
    assert "audio transcriber" in llm.captured[0]["prompt"]
    assert "Samsung Notes" not in llm.captured[0]["prompt"]


def test_multimodal_passthrough_for_markdown_skips_llm():
    """``.md`` files are decoded directly — no LLM call, no cost."""
    llm = _FakeLLM(response="should not be called")
    extractor = GeminiMultimodalExtractor(llm=llm)

    md_bytes = b"# A note\n\nSome paragraph.\n"
    result = extractor.extract(data=md_bytes, mime_type="text/markdown", file_name="quick.md")

    assert result.method == ExtractionMethod.MARKDOWN_PASSTHROUGH
    assert result.markdown == "# A note\n\nSome paragraph."
    assert result.confidence == 1.0
    assert llm.captured == []  # LLM NOT called


def test_multimodal_passthrough_for_google_doc_skips_llm():
    """Google Doc bytes arrive already exported as MD; method tag preserves provenance."""
    llm = _FakeLLM(response="should not be called")
    extractor = GeminiMultimodalExtractor(llm=llm)

    doc_md_bytes = b"# Doc heading\n\nBody.\n"
    result = extractor.extract(
        data=doc_md_bytes,
        mime_type="application/vnd.google-apps.document",
        file_name="my doc",
    )

    assert result.method == ExtractionMethod.GEMINI_FLASH_DOC_EXPORT
    assert result.markdown == "# Doc heading\n\nBody."
    assert llm.captured == []


def test_multimodal_returns_failed_for_unsupported_mime():
    """Unknown MIME types fail loud rather than mis-route."""
    llm = _FakeLLM(response="should not be called")
    extractor = GeminiMultimodalExtractor(llm=llm)

    result = extractor.extract(
        data=b"\x00\x01", mime_type="application/octet-stream", file_name="x"
    )

    assert result.method == ExtractionMethod.FAILED
    assert "unsupported MIME type" in (result.notes or "")
    assert llm.captured == []


def test_multimodal_returns_failed_for_undecodable_markdown_bytes():
    """Passthrough decodes UTF-8; non-UTF-8 input fails clean."""
    llm = _FakeLLM()
    extractor = GeminiMultimodalExtractor(llm=llm)

    result = extractor.extract(
        data=b"\xff\xfe\x00",
        mime_type="text/markdown",
        file_name="binary.md",
    )

    assert result.method == ExtractionMethod.FAILED
    assert "could not decode" in (result.notes or "")


def test_multimodal_returns_failed_for_empty_markdown_body():
    """Whitespace-only markdown is failure, not an empty success row."""
    llm = _FakeLLM()
    extractor = GeminiMultimodalExtractor(llm=llm)

    result = extractor.extract(
        data=b"   \n\n\t",
        mime_type="text/markdown",
        file_name="blank.md",
    )

    assert result.method == ExtractionMethod.FAILED
    assert result.notes == "empty markdown body"


def test_audio_prompt_mentions_timestamps_and_inaudible_marker():
    """Sanity: the audio prompt is the contract with the model for STT."""
    assert "transcriber" in AUDIO_PROMPT
    assert "[mm:ss]" in AUDIO_PROMPT
    assert "[inaudible]" in AUDIO_PROMPT
    assert "EXTRACTION_META" in AUDIO_PROMPT
