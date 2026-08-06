"""Versioned prompt loader.

PRD §6.1 mandates prompts live on disk under `prompts/`, never inline strings.
Subdirectory-per-agent, file-per-version: `prompts/triage/v1.md`. WS-G agents
load by name + version so prompt rollouts are auditable in PR diffs.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PROMPTS_DIR_ENV = "TB_PROMPTS_DIR"


class PromptNotFound(FileNotFoundError):
    pass


def _resolve_prompts_dir(prompts_dir: Path | None) -> Path:
    if prompts_dir is not None:
        return prompts_dir
    env_dir = os.environ.get(_DEFAULT_PROMPTS_DIR_ENV)
    if env_dir:
        return Path(env_dir)
    # src/agency_brain/common/prompts.py -> sibling agency_brain/prompts/.
    # Bundling prompts inside the package means they ship with the deploy
    # artifact (Vertex AI Agent Engine) without needing extra_packages
    # gymnastics.
    return Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, version: str, prompts_dir: Path | None = None) -> str:
    """Load `prompts/{name}/{version}.md`. Raises PromptNotFound if missing."""
    base = _resolve_prompts_dir(prompts_dir)
    path = base / name / f"{version}.md"
    if not path.is_file():
        raise PromptNotFound(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")
