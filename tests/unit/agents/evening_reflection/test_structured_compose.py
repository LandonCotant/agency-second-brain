"""Unit tests for ADR 0040 §5 structured_compose — schema shape +
parse_extraction_payload tolerance for missing optional fields."""

from __future__ import annotations

import json

import pytest
from agency_brain.agents.evening_reflection.structured_compose import (
    REFLECT_RESPONSE_SCHEMA,
    ParseError,
    parse_extraction_payload,
)

# --------------------------------------------------- schema shape sanity


def test_response_schema_uses_openapi_subset_uppercase_types():
    """Vertex's response_schema is the OpenAPI Schema Object subset —
    types are uppercase, nullable: true is used instead of type lists."""
    s = REFLECT_RESPONSE_SCHEMA
    assert s["type"] == "OBJECT"
    assert s["properties"]["commentary"]["type"] == "STRING"
    assert s["properties"]["decisions"]["type"] == "ARRAY"
    assert s["properties"]["decisions"]["nullable"] is True
    # commentary is the only required top-level field.
    assert s["required"] == ["commentary"]


def test_response_schema_decision_item_requires_title_only():
    s = REFLECT_RESPONSE_SCHEMA
    decision_item = s["properties"]["decisions"]["items"]
    assert decision_item["type"] == "OBJECT"
    assert decision_item["required"] == ["title"]
    # context + source_voice_note_id are nullable, not required.
    assert decision_item["properties"]["context"]["nullable"] is True
    assert decision_item["properties"]["source_voice_note_id"]["nullable"] is True


# --------------------------------------------------- parse_extraction_payload


def test_parse_full_payload():
    raw = json.dumps(
        {
            "commentary": "Today was real.",
            "decisions": [
                {
                    "title": "Renew Client A",
                    "context": "Q3 expires soon",
                    "source_voice_note_id": "captures-recA",
                }
            ],
            "wins": [
                {
                    "title": "Closed ClientC Q3",
                    "summary": "Done deal",
                    "source_voice_note_id": None,
                }
            ],
            "todos": [
                {"body": "Email Alice about the brief", "source_voice_note_id": "captures-recB"}
            ],
        }
    )
    payload = parse_extraction_payload(raw)
    assert payload.commentary == "Today was real."
    assert len(payload.decisions) == 1
    assert payload.decisions[0].title == "Renew Client A"
    assert payload.decisions[0].source_voice_note_id == "captures-recA"
    assert len(payload.wins) == 1
    assert payload.wins[0].source_voice_note_id is None
    assert len(payload.todos) == 1
    assert payload.todos[0].body == "Email Alice about the brief"


def test_parse_payload_with_only_commentary():
    """Empty arrays / missing arrays are both valid — quiet days should
    return commentary only and parse cleanly."""
    raw = json.dumps({"commentary": "Today was unremarkable."})
    payload = parse_extraction_payload(raw)
    assert payload.commentary == "Today was unremarkable."
    assert payload.decisions == ()
    assert payload.wins == ()
    assert payload.todos == ()


def test_parse_payload_with_explicit_nulls():
    raw = json.dumps({"commentary": "x", "decisions": None, "wins": None, "todos": None})
    payload = parse_extraction_payload(raw)
    assert payload.decisions == ()
    assert payload.wins == ()
    assert payload.todos == ()


def test_parse_payload_strips_invalid_array_entries():
    """Non-dict entries in an array are skipped silently — defense-in-depth
    against an SDK quirk that streams partial objects."""
    raw = json.dumps(
        {
            "commentary": "x",
            "decisions": [
                {"title": "Valid one"},
                "garbage",
                None,
                {"title": "Another valid"},
            ],
        }
    )
    payload = parse_extraction_payload(raw)
    titles = [d.title for d in payload.decisions]
    assert titles == ["Valid one", "Another valid"]


def test_parse_payload_raises_on_invalid_json():
    with pytest.raises(ParseError):
        parse_extraction_payload("not json at all")


def test_parse_payload_raises_when_root_is_not_object():
    with pytest.raises(ParseError):
        parse_extraction_payload(json.dumps([1, 2, 3]))


def test_parse_payload_raises_when_commentary_missing():
    raw = json.dumps({"decisions": []})
    with pytest.raises(ParseError):
        parse_extraction_payload(raw)


def test_parse_payload_raises_when_commentary_empty_string():
    raw = json.dumps({"commentary": "   "})
    with pytest.raises(ParseError):
        parse_extraction_payload(raw)


def test_parse_payload_raises_when_decision_missing_title():
    raw = json.dumps(
        {
            "commentary": "x",
            "decisions": [{"context": "no title here"}],
        }
    )
    with pytest.raises(ParseError):
        parse_extraction_payload(raw)


def test_parse_payload_strips_whitespace_from_optional_fields():
    raw = json.dumps(
        {
            "commentary": "  prose  ",
            "decisions": [{"title": "Renew Client A", "context": "  ", "source_voice_note_id": ""}],
        }
    )
    payload = parse_extraction_payload(raw)
    # Commentary trimmed.
    assert payload.commentary == "prose"
    # Whitespace-only context becomes None; empty source_voice_note_id becomes None.
    assert payload.decisions[0].context is None
    assert payload.decisions[0].source_voice_note_id is None
