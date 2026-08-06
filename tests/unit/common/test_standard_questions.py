"""Tests for ``agency_brain.common.standard_questions``."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from agency_brain.common.standard_questions import (
    Question,
    StandardQuestions,
    StandardQuestionsLoadError,
    load_standard_questions,
)


def _write_yaml(tmp_path: Path, name: str, body: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "standard_questions.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_standard_questions_happy_path(tmp_path: Path) -> None:
    yaml_text = """
questions:
  - id: q1
    prompt: What worked?
    weight: 1.0
  - id: q2
    prompt: What didn't?
    weight: 0.5
rotation:
  mode: all
  sample_n: 4
"""
    _write_yaml(tmp_path, "evening_reflection", yaml_text)
    qs = load_standard_questions("evening_reflection", root=tmp_path)
    assert qs.mode == "all"
    assert qs.sample_n == 4
    assert len(qs.questions) == 2
    assert qs.questions[0] == Question(id="q1", prompt="What worked?", weight=1.0)
    assert qs.questions[1].weight == 0.5


def test_load_standard_questions_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StandardQuestionsLoadError):
        load_standard_questions("nope", root=tmp_path)


def test_load_standard_questions_missing_id_raises(tmp_path: Path) -> None:
    yaml_text = """
questions:
  - prompt: orphan
"""
    _write_yaml(tmp_path, "evening_reflection", yaml_text)
    with pytest.raises(StandardQuestionsLoadError):
        load_standard_questions("evening_reflection", root=tmp_path)


def test_load_standard_questions_unknown_rotation_falls_back(tmp_path: Path) -> None:
    yaml_text = """
questions:
  - id: q
    prompt: x
rotation:
  mode: nonsense
"""
    _write_yaml(tmp_path, "evening_reflection", yaml_text)
    qs = load_standard_questions("evening_reflection", root=tmp_path)
    assert qs.mode == "all"


def test_select_all_returns_every_question() -> None:
    qs = StandardQuestions(
        questions=(
            Question(id="a", prompt="a"),
            Question(id="b", prompt="b"),
            Question(id="c", prompt="c"),
        ),
        mode="all",
    )
    assert tuple(q.id for q in qs.select()) == ("a", "b", "c")


def test_select_sample_returns_n_distinct() -> None:
    qs = StandardQuestions(
        questions=tuple(Question(id=str(i), prompt=str(i)) for i in range(6)),
        mode="sample",
        sample_n=3,
    )
    picked = qs.select(rng=random.Random(42))
    assert len(picked) == 3
    assert len({q.id for q in picked}) == 3


def test_select_sample_n_bigger_than_pool_returns_all() -> None:
    qs = StandardQuestions(
        questions=(Question(id="x", prompt="x"),),
        mode="sample",
        sample_n=10,
    )
    assert tuple(q.id for q in qs.select()) == ("x",)
