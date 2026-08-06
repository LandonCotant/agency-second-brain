"""Unit tests for the Brag Spotter composer + payload parser (ADR 0043 §4)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from agency_brain.agents.brag_spotter.composer import (
    BragSpotterComposer,
    ParseError,
    parse_payload,
    render_section_blocks,
)
from agency_brain.agents.brag_spotter.models import (
    ExistingWinRow,
    NoteRow,
    TriagedItemRow,
)

# ---------------------------------------------------------------- parser


def test_parse_payload_minimal_commentary_only():
    raw = json.dumps({"commentary": "Quiet week.", "candidates": []})
    payload = parse_payload(raw)
    assert payload.commentary == "Quiet week."
    assert payload.candidates == ()


def test_parse_payload_with_candidates():
    raw = json.dumps(
        {
            "commentary": "Active week.",
            "candidates": [
                {
                    "title": "Closed Acme",
                    "summary": "5-day loop",
                    "source_kind": "decision",
                    "source_id": "d-1",
                    "evidence_links": ["https://x/y"],
                }
            ],
        }
    )
    payload = parse_payload(raw)
    assert len(payload.candidates) == 1
    c = payload.candidates[0]
    assert c.title == "Closed Acme"
    assert c.source_kind == "decision"
    assert c.evidence_links == ("https://x/y",)


def test_parse_payload_drops_invalid_source_kind():
    raw = json.dumps(
        {
            "commentary": "x",
            "candidates": [
                {"title": "ok", "source_kind": "decision", "source_id": "d-1"},
                {"title": "bad", "source_kind": "INVALID", "source_id": "x"},
            ],
        }
    )
    payload = parse_payload(raw)
    assert len(payload.candidates) == 1
    assert payload.candidates[0].title == "ok"


def test_parse_payload_drops_missing_required_fields():
    raw = json.dumps(
        {
            "commentary": "x",
            "candidates": [
                {"title": "missing-kind", "source_id": "d-1"},
                {"title": "", "source_kind": "decision", "source_id": "d-1"},
                {"title": "missing-id", "source_kind": "decision"},
            ],
        }
    )
    payload = parse_payload(raw)
    assert payload.candidates == ()


def test_parse_payload_raises_on_missing_commentary():
    raw = json.dumps({"candidates": []})
    with pytest.raises(ParseError, match="commentary"):
        parse_payload(raw)


def test_parse_payload_raises_on_invalid_json():
    with pytest.raises(ParseError, match="not valid JSON"):
        parse_payload("not json")


def test_parse_payload_raises_on_non_object():
    with pytest.raises(ParseError, match="not a JSON object"):
        parse_payload("[]")


def test_parse_payload_raises_on_non_list_candidates():
    raw = json.dumps({"commentary": "x", "candidates": {"not": "a list"}})
    with pytest.raises(ParseError, match="candidates"):
        parse_payload(raw)


def test_parse_payload_evidence_links_filters_non_strings():
    raw = json.dumps(
        {
            "commentary": "x",
            "candidates": [
                {
                    "title": "ok",
                    "source_kind": "note",
                    "source_id": "n-1",
                    "evidence_links": ["https://a", 42, None, "https://b"],
                }
            ],
        }
    )
    payload = parse_payload(raw)
    assert payload.candidates[0].evidence_links == ("https://a", "https://b")


# ---------------------------------------------------------------- block render


def test_render_section_blocks_handles_empty_inputs():
    blocks = render_section_blocks(
        triaged_items=[],
        routed_events=[],
        notes=[],
        decisions=[],
        reflections=[],
        existing_wins=[],
    )
    for key, value in blocks.items():
        assert value == "(none)", f"{key} should render as (none) when empty"


def test_render_triaged_items_block_includes_severity_and_url():
    blocks = render_section_blocks(
        triaged_items=[
            TriagedItemRow(
                item_id="i-1",
                triaged_at=datetime(2026, 5, 4, tzinfo=UTC),
                severity="critical",
                reasoning="Hot lead",
                source="gmail",
                source_url="https://x/y",
                positive_goal_achieving="close-deal",
            )
        ],
        routed_events=[],
        notes=[],
        decisions=[],
        reflections=[],
        existing_wins=[],
    )
    line = blocks["triaged_items_block"]
    assert "i-1" in line
    assert "critical" in line
    assert "Hot lead" in line
    assert "https://x/y" in line
    assert "close-deal" in line


def test_render_existing_wins_block_lists_titles():
    blocks = render_section_blocks(
        triaged_items=[],
        routed_events=[],
        notes=[],
        decisions=[],
        reflections=[],
        existing_wins=[
            ExistingWinRow(
                win_id="w-1",
                title="Already captured",
                source_kind="reflection",
                source_id="r-1",
            )
        ],
    )
    assert "Already captured" in blocks["existing_wins_block"]
    assert "reflection" in blocks["existing_wins_block"]


def test_render_notes_block_truncates_long_content():
    long_md = "x" * 1000
    blocks = render_section_blocks(
        triaged_items=[],
        routed_events=[],
        notes=[
            NoteRow(
                note_id="n-1",
                ingested_at=datetime(2026, 5, 4, tzinfo=UTC),
                filename="f.pdf",
                extraction_method="gemini-2.5-flash-pdf",
                markdown_content=long_md,
                source_drive_url="https://drive/x",
            )
        ],
        decisions=[],
        reflections=[],
        existing_wins=[],
    )
    block = blocks["notes_block"]
    # Snippet truncated to 240 chars (renderer caps it).
    assert len([line for line in block.splitlines() if "x" * 240 in line]) == 1


# ---------------------------------------------------------------- composer


@dataclass
class _FakeLLM:
    canned: str = ""
    last_prompt: str = ""

    def generate(self, *, prompt: str, model: str) -> str:
        self.last_prompt = prompt
        return self.canned


def test_composer_substitutes_template_and_parses_response():
    template = "Recipient: {{recipient_email}}, Week: {{week_of_human}}\nT:{{triaged_items_block}}\nE:{{existing_wins_block}}\nR:{{routed_events_block}}\nN:{{notes_block}}\nD:{{decisions_block}}\nF:{{reflections_block}}"
    llm = _FakeLLM(
        canned=json.dumps(
            {
                "commentary": "Did stuff.",
                "candidates": [
                    {
                        "title": "Shipped X",
                        "source_kind": "note",
                        "source_id": "n-1",
                    }
                ],
            }
        )
    )
    composer = BragSpotterComposer(prompt_template=template, llm=llm)
    payload = composer.compose(
        recipient_email="owner@example.com",
        week_of=date(2026, 5, 4),
        triaged_items=[],
        routed_events=[],
        notes=[],
        decisions=[],
        reflections=[],
        existing_wins=[],
    )
    assert "owner@example.com" in llm.last_prompt
    assert "May 04, 2026" in llm.last_prompt
    assert payload.commentary == "Did stuff."
    assert len(payload.candidates) == 1
    assert payload.candidates[0].source_kind == "note"
