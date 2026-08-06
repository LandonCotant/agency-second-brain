"""Tests for ``people_sync.frontmatter`` — YAML roundtrip + diff."""

from __future__ import annotations

from agency_brain.agents.people_sync.frontmatter import (
    compose,
    diff_relevant,
    dump_frontmatter,
    parse_file,
)


def test_dump_then_parse_roundtrips_simple_dict() -> None:
    src = {
        "type": "person",
        "name": "First Last",
        "warmth": "warm",
        "last_contact": "2026-03-28",
    }
    text = dump_frontmatter(src)
    parsed, body = parse_file(text)
    assert parsed == src
    assert body == ""


def test_parse_splits_frontmatter_from_body() -> None:
    content = """---
type: person
name: "Sam Q"
warmth: warm
---

# Sam Q

## Who they are
A delightful human.
"""
    parsed, body = parse_file(content)
    assert parsed["name"] == "Sam Q"
    assert parsed["warmth"] == "warm"
    assert body.startswith("# Sam Q")
    assert "A delightful human." in body


def test_parse_returns_empty_dict_when_no_frontmatter() -> None:
    content = "# Plain markdown, no frontmatter\n\nBody here.\n"
    parsed, body = parse_file(content)
    assert parsed == {}
    assert body == content


def test_parse_tolerates_missing_closing_delimiter() -> None:
    content = """---
type: person
name: "Broken"
"""
    parsed, body = parse_file(content)
    # No closing delimiter → whole thing treated as body, frontmatter empty.
    assert parsed == {}
    assert body == content


def test_diff_relevant_ignores_synced_at() -> None:
    old = {"type": "person", "warmth": "warm", "synced_at": "2026-05-17T..."}
    new = {"type": "person", "warmth": "warm", "synced_at": "2026-05-18T..."}
    assert diff_relevant(new, old) is False


def test_diff_relevant_detects_actual_change() -> None:
    old = {"warmth": "warm", "synced_at": "2026-05-17T..."}
    new = {"warmth": "cool", "synced_at": "2026-05-18T..."}
    assert diff_relevant(new, old) is True


def test_diff_relevant_detects_added_key() -> None:
    old = {"warmth": "warm"}
    new = {"warmth": "warm", "tags": ["ross"]}
    assert diff_relevant(new, old) is True


def test_diff_relevant_detects_removed_key() -> None:
    old = {"warmth": "warm", "linkedin": "https://..."}
    new = {"warmth": "warm"}
    assert diff_relevant(new, old) is True


def test_compose_emits_block_blank_line_body() -> None:
    fm = {"type": "person", "name": "X"}
    body = "# X\n\nContent.\n"
    out = compose(fm, body)
    assert out.startswith("---\ntype: person")
    assert "---\n\n# X" in out
    # Roundtrip
    parsed, parsed_body = parse_file(out)
    assert parsed == fm
    assert parsed_body == body


def test_dump_preserves_key_order() -> None:
    src = {"alpha": 1, "beta": 2, "gamma": 3}
    text = dump_frontmatter(src)
    # YAML lines should be in insertion order.
    assert text.index("alpha") < text.index("beta") < text.index("gamma")
