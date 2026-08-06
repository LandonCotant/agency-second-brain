from __future__ import annotations

from pathlib import Path

import pytest
from agency_brain.common.prompts import PromptNotFound, load_prompt


def _write(prompts_dir: Path, name: str, version: str, body: str) -> None:
    target = prompts_dir / name / f"{version}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_load_prompt_returns_file_contents(tmp_path: Path) -> None:
    _write(tmp_path, "triage", "v1", "# Triage prompt\nClassify this.\n")

    assert load_prompt("triage", "v1", prompts_dir=tmp_path) == (
        "# Triage prompt\nClassify this.\n"
    )


def test_load_prompt_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFound):
        load_prompt("triage", "v999", prompts_dir=tmp_path)


def test_load_prompt_uses_env_when_dir_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "triage", "v1", "from env")
    monkeypatch.setenv("TB_PROMPTS_DIR", str(tmp_path))

    assert load_prompt("triage", "v1") == "from env"
