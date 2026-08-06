"""Tests for ``LibrarianClassifier``."""

from __future__ import annotations

import json

import pytest
from agency_brain.agents.librarian.classifier import (
    ClassifyError,
    LibrarianClassifier,
    LibrarianClassifyConfig,
    _parse_classification,
)
from agency_brain.agents.librarian.models import AreaFolder


class _FakeLLM:
    def __init__(self, *, response: str | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def generate(self, *, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _config() -> LibrarianClassifyConfig:
    return LibrarianClassifyConfig(project_id="p")


def _candidates() -> list[AreaFolder]:
    return [
        AreaFolder(id="i1", name="clienta-pi", path="clients/clienta-pi"),
        AreaFolder(id="i2", name="jro", path="clients/client-c-studio"),
        AreaFolder(id="i3", name="local-service", path="playbooks/local-service"),
    ]


def test_classify_picks_path_from_candidates() -> None:
    llm = _FakeLLM(
        response=json.dumps(
            {
                "dest_folder_path": "clients/clienta-pi",
                "confidence": 0.84,
                "reasoning": "names Client A three times",
            }
        )
    )
    cls = LibrarianClassifier(llm=llm, config=_config())
    result = cls.classify(
        filename="memo-clienta.md",
        extracted_markdown="Client A's lead-gen renewal is overdue.",
        candidates=_candidates(),
    )
    assert result.dest_folder_path == "clients/clienta-pi"
    assert result.confidence == 0.84
    assert "Client A" in result.reasoning


def test_classify_short_circuits_when_no_candidates() -> None:
    llm = _FakeLLM(response="UNUSED")
    cls = LibrarianClassifier(llm=llm, config=_config())
    result = cls.classify(
        filename="x.pdf",
        extracted_markdown="content",
        candidates=[],
    )
    assert result.dest_folder_path is None
    assert result.confidence == 0.0
    assert llm.calls == []  # never called when there are no candidates


def test_classify_clamps_confidence_to_unit_interval() -> None:
    llm = _FakeLLM(
        response=json.dumps({"dest_folder_path": None, "confidence": 1.7, "reasoning": "uncertain"})
    )
    cls = LibrarianClassifier(llm=llm, config=_config())
    result = cls.classify(filename="x.md", extracted_markdown="content", candidates=_candidates())
    assert result.confidence == 1.0


def test_classify_raises_on_llm_failure() -> None:
    llm = _FakeLLM(response=RuntimeError("vertex unreachable"))
    cls = LibrarianClassifier(llm=llm, config=_config())
    with pytest.raises(ClassifyError):
        cls.classify(filename="x.md", extracted_markdown="content", candidates=_candidates())


def test_parse_classification_rejects_non_json() -> None:
    with pytest.raises(ClassifyError):
        _parse_classification("not json at all")


def test_parse_classification_handles_null_dest() -> None:
    raw = json.dumps({"dest_folder_path": None, "confidence": 0.4, "reasoning": "no fit"})
    parsed = _parse_classification(raw)
    assert parsed.dest_folder_path is None
    assert parsed.confidence == 0.4


def test_parse_classification_handles_blank_dest() -> None:
    raw = json.dumps({"dest_folder_path": "   ", "confidence": 0.4, "reasoning": ""})
    parsed = _parse_classification(raw)
    assert parsed.dest_folder_path is None
    assert parsed.reasoning == "(no reasoning provided)"
