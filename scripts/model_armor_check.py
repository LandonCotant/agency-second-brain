#!/usr/bin/env python3
"""Confirm agents listed in PRD §4.4 have a Model Armor Template provisioned.

Enforces PRD §4.8 PR security gate.

Required-armor agents (PRD §4.4):
- Triage Agent — `asb-agent-triage*`
- Knowledge Surfacer — `asb-agent-knowledge-surfacer*`
- Risk Watcher (all profiles) — `asb-agent-risk-watcher*` (covers `-ecommerce`,
  `-local`, `-agency-partner`, and any future variants).

## How Model Armor is wired in 2026

Model Armor is NOT a sub-block of `google_vertex_ai_reasoning_engine` — that
field has never existed in the provider. The correct shape is a separate
`google_model_armor_template` resource that defines filter rules
(prompt-injection / jailbreak, malicious URI, Responsible AI). Agents would
apply the Template by passing its name in their `generate_content` call.
ADR 0015 captures the full architecture.

**Runtime caveat (ADR 0017, 2026-04-30):** the Triage Agent currently runs
with `TB_ENABLE_MODEL_ARMOR=false` because the Reasoning Engine SA hits
`IAM_PERMISSION_DENIED` on the Template at request time and the right role
hasn't been pinned down. The Template + role binding remain in TF as no-ops.
This script's scope is provision-time only — it doesn't (and shouldn't)
verify runtime enforcement. If/when Model Armor is re-enabled, the runtime
check belongs in agent unit tests, not here.

## Pass criterion

For each `google_vertex_ai_reasoning_engine` resource whose `display_name`
(or `name`) matches a required-armor pattern, the Terraform must contain a
`google_model_armor_template` whose `template_id` matches the same pattern.
Additionally, *any* required-armor Template that does exist must have a
`filter_config { ... }` containing at least one filter sub-block — so an
empty `filter_config { }` doesn't slip through.

This is RE-keyed by design. Workstreams that haven't shipped their agent yet
(no RE, no Template) don't fail the gate — but the moment they add an RE
they must also add a Template. Equivalently, a Template can be staged
ahead of the RE without a violation, as long as its filter_config is real.

Limitations: the brace counter doesn't strip strings/heredocs, so an HCL
string containing unbalanced `{` or `}` could confuse block extraction.
Acceptable for this repo today; revisit if WS-G introduces unusual HCL.

Usage:
    python scripts/model_armor_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = REPO_ROOT / "terraform"

REQUIRED_ARMOR_NAME_PATTERNS = (
    re.compile(r"^asb-agent-triage(?:-|$)"),
    # Knowledge Surfacer pattern removed per ADR 0059 (service retired).
    re.compile(r"^asb-agent-risk-watcher(?:-|$)"),
)

REASONING_ENGINE_RESOURCE_PATTERN = re.compile(
    r'resource\s+"google_vertex_ai_reasoning_engine"\s+"([^"]+)"\s*\{',
    re.MULTILINE,
)

ARMOR_TEMPLATE_RESOURCE_PATTERN = re.compile(
    r'resource\s+"google_model_armor_template"\s+"([^"]+)"\s*\{',
    re.MULTILINE,
)

NAME_ATTR_PATTERN = re.compile(
    r'(?:^|\s)(?:display_name|name)\s*=\s*"([^"]+)"',
    re.MULTILINE,
)

TEMPLATE_ID_PATTERN = re.compile(
    r'(?:^|\s)template_id\s*=\s*"([^"]+)"',
    re.MULTILINE,
)

FILTER_CONFIG_BLOCK_PATTERN = re.compile(r"filter_config\s*\{", re.MULTILINE)

# Any of these sub-blocks (empty or not) inside filter_config counts as a
# present filter. Empty filter_config { } fails the gate.
FILTER_SUB_BLOCKS = (
    "pi_and_jailbreak_filter_settings",
    "malicious_uri_filter_settings",
    "rai_settings",
    "sdp_settings",
)
FILTER_SUB_BLOCK_PATTERN = re.compile(
    r"\b(" + "|".join(FILTER_SUB_BLOCKS) + r")\s*\{", re.MULTILINE
)


def _extract_block(content: str, brace_open_index: int) -> str:
    """Return the substring spanning the matched braces of a block whose opening
    `{` is at `brace_open_index`."""
    depth = 0
    i = brace_open_index
    while i < len(content):
        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[brace_open_index : i + 1]
        i += 1
    return content[brace_open_index:]


def _required_armor(template_id: str) -> bool:
    return any(p.match(template_id) for p in REQUIRED_ARMOR_NAME_PATTERNS)


def _template_id_from_block(block: str) -> str | None:
    m = TEMPLATE_ID_PATTERN.search(block)
    return m.group(1) if m else None


def _has_filter(template_block: str) -> bool:
    fc_match = FILTER_CONFIG_BLOCK_PATTERN.search(template_block)
    if not fc_match:
        return False
    fc_block = _extract_block(template_block, fc_match.end() - 1)
    return bool(FILTER_SUB_BLOCK_PATTERN.search(fc_block))


def _display_path(tf_path: Path) -> str:
    try:
        return str(tf_path.relative_to(REPO_ROOT))
    except ValueError:
        return str(tf_path)


def _agent_name_from_block(block: str) -> str | None:
    m = NAME_ATTR_PATTERN.search(block)
    return m.group(1) if m else None


def find_violations(root: Path = TERRAFORM_DIR) -> list[str]:
    """Two violation types:
    1. An RE resource matches a required-armor pattern but no Template's
       template_id covers it.
    2. A required-armor Template exists but its filter_config is empty.

    Workstreams that haven't shipped their agent yet (no RE, no Template)
    don't fail the gate.
    """
    violations: list[str] = []
    if not root.exists():
        return []

    # Index Templates first.
    found_templates: list[tuple[str, Path, str]] = []
    # Index required-armor REs.
    found_required_res: list[tuple[str, str, Path]] = []  # (tf_name, agent_name, path)

    for tf_path in root.rglob("*.tf"):
        content = tf_path.read_text(errors="replace")
        for match in ARMOR_TEMPLATE_RESOURCE_PATTERN.finditer(content):
            block = _extract_block(content, match.end() - 1)
            template_id = _template_id_from_block(block)
            if template_id is None:
                continue
            if _required_armor(template_id):
                found_templates.append((template_id, tf_path, block))

        for match in REASONING_ENGINE_RESOURCE_PATTERN.finditer(content):
            tf_name = match.group(1)
            block = _extract_block(content, match.end() - 1)
            agent_name = _agent_name_from_block(block)
            if agent_name and _required_armor(agent_name):
                found_required_res.append((tf_name, agent_name, tf_path))

    # Violation type 2: Template exists but no filter sub-block.
    for tid, path, block in found_templates:
        if not _has_filter(block):
            violations.append(
                f"{_display_path(path)}: model_armor_template "
                f"(template_id={tid!r}) has empty or missing filter_config "
                f"(PRD §4.4)"
            )

    # Violation type 1: required-armor RE has no matching Template.
    template_ids = [tid for tid, _, _ in found_templates]
    for tf_name, agent_name, path in found_required_res:
        # Does any Template's template_id share the same required-armor pattern?
        matching_pattern = next(
            (p for p in REQUIRED_ARMOR_NAME_PATTERNS if p.match(agent_name)),
            None,
        )
        if matching_pattern is None:
            continue  # shouldn't happen given the filter above
        if not any(matching_pattern.match(tid) for tid in template_ids):
            violations.append(
                f"{_display_path(path)}: reasoning_engine.{tf_name} "
                f"(name={agent_name!r}) has no covering "
                f"google_model_armor_template (PRD §4.4)"
            )
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("Model Armor check FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("Model Armor check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
