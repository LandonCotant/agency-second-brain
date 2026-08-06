"""YAML loader for daily reflection's standard questions.

The Evening Reflection Doc (REFLECT mode, ADR 0040 + 0044) renders two
question blocks:

- **Standard questions** — a hand-curated list anchored across days;
  loaded from ``prompts/<name>/standard_questions.yaml``. The prompt is
  rendered deterministically into the Doc (no LLM call). Editing the
  YAML and shipping a PR is the only way to change them.
- **Custom questions** — 2-3 LLM-generated per day from that day's
  themes (different module: lives inside the structured-extraction
  payload at composer call time).

This module owns the standard set: shape, parse, and surface a
deterministic list. The Vertex prompt template never sees these
questions — they're rendered straight into the Doc body.

YAML shape:

    questions:
      - id: worked_today
        prompt: "What worked today that's worth repeating?"
        weight: 1.0
      - id: didnt_work
        prompt: "What didn't work, and what's the smallest experiment to fix it?"
    rotation:
      mode: all       # 'all' or 'sample'
      sample_n: 4     # used only when mode='sample'

``mode='all'`` returns every question every day; ``mode='sample'`` would
weighted-pick ``sample_n`` and rotate. v1 uses ``all`` — the ritual
matters more than novelty.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agency_brain.common.standard_questions")


_PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Question:
    """One standard reflection question."""

    id: str
    prompt: str
    weight: float = 1.0


@dataclass(frozen=True)
class StandardQuestions:
    """Loaded questions + rotation policy."""

    questions: tuple[Question, ...]
    mode: str = "all"
    """``all`` returns every question; ``sample`` returns ``sample_n``
    weight-sampled questions (deterministic seed not needed in v1)."""
    sample_n: int = 4

    def select(self, *, rng: random.Random | None = None) -> tuple[Question, ...]:
        """Resolve the rotation policy into a concrete list for today's render.

        ``mode='all'`` returns every loaded question in declared order.
        ``mode='sample'`` weight-samples ``sample_n`` questions without
        replacement; uses the supplied ``rng`` (or builds a fresh one)
        so callers can pass a seeded RNG for stability.
        """
        if not self.questions:
            return ()
        if self.mode == "sample" and self.sample_n > 0 and self.sample_n < len(self.questions):
            picker = rng or random.Random()  # noqa: S311 - reflective UX, not crypto
            weights = [max(0.0, q.weight) for q in self.questions]
            if sum(weights) <= 0:
                weights = [1.0] * len(self.questions)
            picked: list[Question] = []
            pool = list(zip(self.questions, weights, strict=True))
            for _ in range(self.sample_n):
                if not pool:
                    break
                total = sum(w for _, w in pool)
                roll = picker.uniform(0, total)
                acc = 0.0
                idx = 0
                for i, (_, w) in enumerate(pool):
                    acc += w
                    if roll <= acc:
                        idx = i
                        break
                picked.append(pool[idx][0])
                pool.pop(idx)
            return tuple(picked)
        return tuple(self.questions)


class StandardQuestionsLoadError(RuntimeError):
    pass


def load_standard_questions(
    name: str,
    *,
    root: Path | None = None,
    filename: str = "standard_questions.yaml",
) -> StandardQuestions:
    """Read ``prompts/<name>/<filename>`` and return parsed ``StandardQuestions``.

    Mirrors ``agency_brain.common.prompts.load_prompt`` for ergonomics:
    callers pass a logical name (e.g. ``evening_reflection``) and the
    file resolves under the in-repo ``prompts/`` tree.
    """
    base = root or _PROMPTS_ROOT
    path = base / name / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StandardQuestionsLoadError(f"standard_questions file not found at {path}") from exc

    try:
        import yaml  # noqa: F401 — Dockerfile.evening-reflection adds PyYAML
    except ImportError as exc:  # pragma: no cover — dependency error path
        raise StandardQuestionsLoadError(
            "PyYAML not installed; add it to the agent's Dockerfile (see "
            "Codebase gotchas in CLAUDE.md)."
        ) from exc

    import yaml as _yaml

    try:
        data = _yaml.safe_load(text) or {}
    except _yaml.YAMLError as exc:
        raise StandardQuestionsLoadError(f"YAML parse failed for {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise StandardQuestionsLoadError(f"top-level YAML must be a mapping at {path}")

    raw_questions = data.get("questions") or []
    if not isinstance(raw_questions, list):
        raise StandardQuestionsLoadError(
            f"'questions' must be a list at {path}, got {type(raw_questions).__name__}"
        )

    questions: list[Question] = []
    for i, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise StandardQuestionsLoadError(f"questions[{i}] must be a mapping at {path}")
        qid = str(raw.get("id") or "").strip()
        prompt_text = str(raw.get("prompt") or "").strip()
        if not qid:
            raise StandardQuestionsLoadError(f"questions[{i}].id is required at {path}")
        if not prompt_text:
            raise StandardQuestionsLoadError(f"questions[{i}].prompt is required at {path}")
        weight_raw = raw.get("weight", 1.0)
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError):
            weight = 1.0
        questions.append(Question(id=qid, prompt=prompt_text, weight=weight))

    rotation = data.get("rotation") or {}
    mode = "all"
    sample_n = 4
    if isinstance(rotation, dict):
        mode = str(rotation.get("mode") or "all").strip().lower()
        if mode not in {"all", "sample"}:
            log.warning("standard_questions: unknown rotation mode %r — defaulting to 'all'", mode)
            mode = "all"
        try:
            sample_n = int(rotation.get("sample_n") or 4)
        except (TypeError, ValueError):
            sample_n = 4

    return StandardQuestions(
        questions=tuple(questions),
        mode=mode,
        sample_n=sample_n,
    )
