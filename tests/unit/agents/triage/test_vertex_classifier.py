"""Unit tests for `VertexClassifier`. Mocks the google.genai client.

PR 4c Phase 2: verifies the model_armor_config + response_schema are
correctly threaded into the generate_content call. No real Vertex hit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from agency_brain.agents.triage.vertex_classifier import (
    TRIAGE_RESPONSE_SCHEMA,
    VertexClassifier,
    VertexClassifierConfig,
)


@dataclass
class _FakeResponse:
    text: str = ""


class _RecordingModels:
    """Stands in for `client.models`."""

    def __init__(self, return_text: str = '{"ok": true}') -> None:
        self.calls: list[dict[str, Any]] = []
        self._return_text = return_text

    def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeResponse(text=self._return_text)


class _FakeClient:
    def __init__(self, return_text: str = '{"ok": true}') -> None:
        self.models = _RecordingModels(return_text=return_text)


def _make_classifier(return_text: str = '{"ok": true}') -> tuple[VertexClassifier, _FakeClient]:
    client = _FakeClient(return_text=return_text)
    config = VertexClassifierConfig(project_id="agency-brain-demo")
    return VertexClassifier(config=config, client=client), client


def test_classify_returns_text_from_response() -> None:
    classifier, _ = _make_classifier(return_text='{"actionable": true}')
    result = classifier.classify(prompt="hello prompt", signal_block='{"a": 1}')
    assert result == '{"actionable": true}'


def test_classify_passes_correct_model_name() -> None:
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")
    assert client.models.calls[0]["model"] == "gemini-2.5-flash"


def test_classify_combines_prompt_and_signal_in_user_turn() -> None:
    classifier, client = _make_classifier()
    classifier.classify(prompt="PROMPT_TXT", signal_block='{"sig": true}')
    contents = client.models.calls[0]["contents"]
    assert isinstance(contents, list)
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    body = contents[0]["parts"][0]["text"]
    assert "PROMPT_TXT" in body
    assert '{"sig": true}' in body


def test_classify_attaches_model_armor_template_to_config() -> None:
    """The most important test in this PR: every call must reference the
    Triage Model Armor Template (PRD §4.4 / ADR 0015 enforcement at the
    API call level, not on the RE resource)."""
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")

    config = client.models.calls[0]["config"]
    armor = config.model_armor_config
    expected = "projects/agency-brain-demo/locations/us-central1/templates/asb-agent-triage"
    assert armor.prompt_template_name == expected
    assert armor.response_template_name == expected


def test_classify_uses_controlled_generation_for_json() -> None:
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")
    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    # response_schema may be passed through as-is or normalized by Pydantic;
    # check that the structural shape we expect is preserved.
    schema = config.response_schema
    assert schema == TRIAGE_RESPONSE_SCHEMA


def test_schema_requires_positive_goal_achieving() -> None:
    # Without this in `required`, gemini-2.5-flash silently drops the field on
    # the actionable=true path and TriageOutput parsing fails — production
    # incident 2026-05-01.
    assert "positive_goal_achieving" in TRIAGE_RESPONSE_SCHEMA["required"]
    assert TRIAGE_RESPONSE_SCHEMA["properties"]["positive_goal_achieving"]["nullable"] is True


def test_classify_uses_low_temperature() -> None:
    """Classification, not creative writing — temperature should be low."""
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")
    assert client.models.calls[0]["config"].temperature == pytest.approx(0.2)


def test_classify_disables_thinking_for_bounded_json() -> None:
    """Structured classification should spend output tokens on JSON, not
    hidden thinking tokens that can truncate the response."""
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")
    thinking_config = client.models.calls[0]["config"].thinking_config
    assert thinking_config.thinking_budget == 0


def test_classify_uses_larger_output_budget() -> None:
    classifier, client = _make_classifier()
    classifier.classify(prompt="p", signal_block="s")
    assert client.models.calls[0]["config"].max_output_tokens == 8192


def test_template_name_format() -> None:
    """The full Model Armor Template resource name format matches what
    `terraform/modules/agent_runtime/triage_model_armor_template.tf` deploys."""
    config = VertexClassifierConfig(project_id="some-project", location="us-east1")
    assert config.model_armor_template_name == (
        "projects/some-project/locations/us-east1/templates/asb-agent-triage"
    )


def test_classify_handles_response_without_text_attribute() -> None:
    """If response.text is missing, the extractor walks candidates.parts."""
    # Build a response shape that mirrors the real google.genai shape:
    # candidates[0].content.parts[0].text
    part = MagicMock()
    part.text = '{"actionable": false}'
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    response = MagicMock()
    response.text = None
    response.candidates = [cand]

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = response

    config = VertexClassifierConfig(project_id="p")
    out = VertexClassifier(config=config, client=fake_client).classify(prompt="p", signal_block="s")
    assert out == '{"actionable": false}'


def test_classify_prefers_parsed_response_when_available() -> None:
    """The SDK may expose schema-constrained output as parsed data; use that
    instead of relying on the convenience text property."""
    response = MagicMock()
    response.parsed = {"actionable": False}
    response.text = '{"ignored": true}'

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = response

    config = VertexClassifierConfig(project_id="p")
    out = VertexClassifier(config=config, client=fake_client).classify(prompt="p", signal_block="s")
    assert out == '{"actionable": false}'


def test_classify_joins_multiple_response_parts() -> None:
    part1 = MagicMock()
    part1.text = '{"actionable":'
    part2 = MagicMock()
    part2.text = " false}"
    content = MagicMock()
    content.parts = [part1, part2]
    cand = MagicMock()
    cand.content = content
    response = MagicMock()
    response.parsed = None
    response.text = None
    response.candidates = [cand]

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = response

    config = VertexClassifierConfig(project_id="p")
    out = VertexClassifier(config=config, client=fake_client).classify(prompt="p", signal_block="s")
    assert out == '{"actionable": false}'
