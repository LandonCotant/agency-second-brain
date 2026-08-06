"""Tests for ``people_sync.sections`` — surgical AUTO-section replacement."""

from __future__ import annotations

from agency_brain.agents.people_sync.sections import (
    AUTO_MARKER,
    replace_auto_section,
)

_BODY = f"""# Client A

## Who they are
User-written prose about Client A. NEVER OVERWRITE THIS.

## Active engagements
{AUTO_MARKER}
- Old project

## Recent activity
{AUTO_MARKER}
- 2026-05-01: stale data

## Open risks
{AUTO_MARKER}
(no open risks)

## Connections
User wikilinks: [[Sam Q]], [[Lou Smith]]
"""


def test_replaces_only_named_auto_section() -> None:
    new_active = f"{AUTO_MARKER}\n- Q3 campaign\n- Site redesign\n"
    out = replace_auto_section(_BODY, "## Active engagements", new_active)
    assert "Q3 campaign" in out
    assert "Site redesign" in out
    # Old auto content gone
    assert "Old project" not in out
    # OTHER sections untouched
    assert "User-written prose about Client A. NEVER OVERWRITE THIS." in out
    assert "2026-05-01: stale data" in out  # recent_activity section unchanged
    assert "User wikilinks: [[Sam Q]], [[Lou Smith]]" in out


def test_protects_user_prose_section_even_if_called_by_mistake() -> None:
    """Calling replace on a NON-auto section returns the body unchanged.

    ADR 0057 §4 invariant: user prose is never overwritten. The function
    only replaces sections whose first non-blank line is the AUTO marker.
    """
    out = replace_auto_section(_BODY, "## Who they are", "ATTACK_CONTENT")
    assert "ATTACK_CONTENT" not in out
    assert "User-written prose about Client A. NEVER OVERWRITE THIS." in out


def test_protects_user_prose_section_with_only_connections() -> None:
    out = replace_auto_section(_BODY, "## Connections", "ATTACK_CONTENT")
    assert "ATTACK_CONTENT" not in out
    assert "[[Sam Q]]" in out


def test_unknown_header_is_noop() -> None:
    out = replace_auto_section(_BODY, "## Does Not Exist", f"{AUTO_MARKER}\nfoo\n")
    assert out == _BODY


def test_idempotent_when_new_content_matches_existing() -> None:
    same = f"{AUTO_MARKER}\n- Old project\n"
    out = replace_auto_section(_BODY, "## Active engagements", same)
    # Output should equal input (or be byte-identical modulo trailing nl handling)
    # The function ensures trailing newline; check semantics not exact bytes.
    assert "- Old project" in out
    # No duplication
    assert out.count("- Old project") == 1


def test_preserves_other_section_boundaries() -> None:
    """After replacement, subsequent ## headers should still be there."""
    new_active = f"{AUTO_MARKER}\n- Refreshed\n"
    out = replace_auto_section(_BODY, "## Active engagements", new_active)
    assert "## Recent activity" in out
    assert "## Open risks" in out
    assert "## Connections" in out
