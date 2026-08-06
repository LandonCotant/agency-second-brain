"""YAML frontmatter serialization + parsing + diff for People Sync (ADR 0057).

A People Sync output file looks like::

    ---
    type: person
    airtable_id: rec...
    name: "First Last"
    warmth: warm
    ...
    synced_at: 2026-05-18T15:00:00Z
    ---

    # First Last

    ## Who they are
    <user prose>

    ...

This module:

  - ``dump_frontmatter(d) -> str`` — emit the YAML block including the
    ``---`` delimiters, deterministic key order so re-emits are stable.
  - ``parse_file(content) -> (dict, body)`` — split an existing file
    into frontmatter dict + body markdown.
  - ``diff_relevant(new, old) -> bool`` — True if any key the sync
    cares about differs. Ignores ``synced_at`` (always changes; only
    bumped when other keys also differ — ADR 0057 §4).
  - ``compose(new_fm, body) -> str`` — assemble final file content.

PyYAML is the runtime dep (pinned in ``Dockerfile.people-sync``).
"""

from __future__ import annotations

from typing import Any

import yaml

FRONTMATTER_DELIMITER = "---"

_IGNORED_DIFF_KEYS = {"synced_at"}
"""Keys excluded from the relevance diff. ``synced_at`` always changes
on each run; bumping it shouldn't trigger a Drive write unless something
else also changed (ADR 0057 §4)."""


def dump_frontmatter(data: dict[str, Any]) -> str:
    """Serialize a frontmatter dict to a YAML block.

    Returns the full block including leading + trailing ``---`` lines
    and a trailing newline. Keys are emitted in insertion order
    (preserved by Python 3.7+ dicts) — callers MUST use the same key
    order each tick for stable output.
    """
    body = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )
    return f"{FRONTMATTER_DELIMITER}\n{body}{FRONTMATTER_DELIMITER}\n"


def parse_file(content: str) -> tuple[dict[str, Any], str]:
    """Split a file's full content into (frontmatter dict, body).

    Returns ``({}, content)`` if the file has no frontmatter block
    (defensive — shouldn't happen for files we wrote, but tolerates
    user-deleted frontmatter).
    """
    if not content.startswith(FRONTMATTER_DELIMITER):
        return ({}, content)
    # Skip the first delimiter line, find the closing delimiter.
    after_open = content[len(FRONTMATTER_DELIMITER) :].lstrip("\n")
    # The closing delimiter is the next line that's exactly ``---`` on
    # its own (possibly with surrounding whitespace).
    lines = after_open.splitlines(keepends=True)
    close_idx: int | None = None
    yaml_lines: list[str] = []
    for i, line in enumerate(lines):
        if line.strip() == FRONTMATTER_DELIMITER:
            close_idx = i
            break
        yaml_lines.append(line)
    if close_idx is None:
        # No closing delimiter — treat the whole thing as body.
        return ({}, content)
    yaml_text = "".join(yaml_lines)
    body = "".join(lines[close_idx + 1 :])
    # Strip one leading newline from body if present (the blank line
    # immediately after the closing delimiter is conventional).
    if body.startswith("\n"):
        body = body[1:]
    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return ({}, content)
    if not isinstance(parsed, dict):
        return ({}, content)
    return (parsed, body)


def diff_relevant(new_fm: dict[str, Any], old_fm: dict[str, Any]) -> bool:
    """Return True iff any non-ignored key differs between new and old.

    Used to decide whether a Drive rewrite is needed. ``synced_at`` is
    always different on a fresh run, but on its own it doesn't justify
    a rewrite (ADR 0057 §4).
    """
    keys = (set(new_fm) | set(old_fm)) - _IGNORED_DIFF_KEYS
    for key in keys:
        if new_fm.get(key) != old_fm.get(key):
            return True
    return False


def compose(new_fm: dict[str, Any], body: str) -> str:
    """Assemble final file content: frontmatter block + blank line + body."""
    block = dump_frontmatter(new_fm)
    if body and not body.startswith("\n"):
        return f"{block}\n{body}"
    return f"{block}{body}"
