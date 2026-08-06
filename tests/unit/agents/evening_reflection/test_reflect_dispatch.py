"""Unit tests for ADR 0040 §6 reflect_dispatch — payload routing and
the dispatch summary block rendering."""

from __future__ import annotations

from dataclasses import dataclass, field

from agency_brain.agents.evening_reflection.extracted_writers import (
    DecisionsWriter,
    WinsWriter,
)
from agency_brain.agents.evening_reflection.models import (
    ExtractedDecision,
    ExtractedTodo,
    ExtractedWin,
    ReflectExtractionPayload,
)
from agency_brain.agents.evening_reflection.reflect_dispatch import (
    dispatch,
    render_dispatch_summary_block,
)
from agency_brain.agents.evening_reflection.triage_publisher import (
    ReflectTriagePublisher,
)


@dataclass
class _FakeBQRows:
    captured: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.captured.append((table_ref, rows))
        return []


@dataclass
class _FakeBQQuery:
    existing: set = field(default_factory=set)

    def query_rows(self, sql, parameters=None):
        if not parameters:
            return []
        return [{"x": 1}] if parameters[0]["value"] in self.existing else []


@dataclass
class _FakePublisher:
    publishes: list = field(default_factory=list)

    def publish(self, topic, data, ordering_key="", **attrs):
        self.publishes.append((topic, data, ordering_key))

        class _Fut:
            def result(self_inner, timeout=None):
                return None

        return _Fut()


def _make_dispatch_args():
    bq = _FakeBQRows()
    bq_q = _FakeBQQuery()
    pub = _FakePublisher()
    decisions_writer = DecisionsWriter(
        bq=bq, bq_query=bq_q, table_ref="proj.agent_outputs.decisions"
    )
    wins_writer = WinsWriter(bq=bq, bq_query=bq_q, table_ref="proj.agent_outputs.wins")
    triage_publisher = ReflectTriagePublisher(
        publisher=pub, topic_path="projects/p/topics/asb-triage-input"
    )
    return bq, bq_q, pub, decisions_writer, wins_writer, triage_publisher


def test_dispatch_routes_each_kind_to_correct_writer_or_publisher():
    bq, _, pub, dw, ww, tp = _make_dispatch_args()
    payload = ReflectExtractionPayload(
        commentary="x",
        decisions=(
            ExtractedDecision(title="Renew Client A", context="Q3", source_voice_note_id=None),
        ),
        wins=(ExtractedWin(title="Closed ClientC", summary="done", source_voice_note_id=None),),
        todos=(ExtractedTodo(body="Email Alice", source_voice_note_id=None),),
    )
    summary = dispatch(
        payload,
        reflection_id="r-1",
        agent_run_id="run-99",
        decisions_writer=dw,
        wins_writer=ww,
        triage_publisher=tp,
    )
    assert summary.decisions_written == 1
    assert summary.wins_written == 1
    assert summary.todos_published == 1
    # Two BQ inserts (one decisions row, one wins row).
    assert len(bq.captured) == 2
    table_refs = [t for t, _ in bq.captured]
    assert "proj.agent_outputs.decisions" in table_refs
    assert "proj.agent_outputs.wins" in table_refs
    # One Pub/Sub publish.
    assert len(pub.publishes) == 1


def test_dispatch_aggregates_dedup_skips():
    bq, bq_q, _, dw, ww, _ = _make_dispatch_args()
    # Pre-populate the dedup set with the decision id we'll emit.
    payload = ReflectExtractionPayload(
        commentary="x",
        decisions=(
            ExtractedDecision(title="Renew Client A", context=None, source_voice_note_id=None),
        ),
    )
    # First dispatch writes the row.
    summary1 = dispatch(
        payload,
        reflection_id="r-1",
        agent_run_id=None,
        decisions_writer=dw,
        wins_writer=ww,
        triage_publisher=None,
    )
    assert summary1.decisions_written == 1
    # Mark the row as existing for the second dispatch (simulating a re-tick).
    written_id = summary1.decision_outcomes[0].target_id
    bq_q.existing.add(written_id)
    summary2 = dispatch(
        payload,
        reflection_id="r-1",
        agent_run_id=None,
        decisions_writer=dw,
        wins_writer=ww,
        triage_publisher=None,
    )
    assert summary2.decisions_written == 0
    assert summary2.decisions_skipped == 1


def test_dispatch_skips_unwired_surfaces_silently():
    """PROMPT-mode tests + degraded REFLECT-mode tick may not wire all
    three surfaces. Missing ones must drop their array silently
    rather than crashing."""
    payload = ReflectExtractionPayload(
        commentary="x",
        decisions=(ExtractedDecision(title="A"),),
        wins=(ExtractedWin(title="B"),),
        todos=(ExtractedTodo(body="C"),),
    )
    summary = dispatch(
        payload,
        reflection_id="r-1",
        agent_run_id=None,
        decisions_writer=None,
        wins_writer=None,
        triage_publisher=None,
    )
    assert summary.decisions_written == 0
    assert summary.wins_written == 0
    assert summary.todos_published == 0
    assert summary.decision_outcomes == ()


# --------------------------------------------------- summary block rendering


def test_render_summary_block_empty_payload_returns_empty_string():
    payload = ReflectExtractionPayload(commentary="x")
    assert render_dispatch_summary_block(payload) == ""


def test_render_summary_block_includes_per_kind_sections():
    payload = ReflectExtractionPayload(
        commentary="x",
        decisions=(
            ExtractedDecision(
                title="Renew Client A", context="Q3", source_voice_note_id="captures-recA"
            ),
        ),
        wins=(ExtractedWin(title="Closed ClientC", summary="done", source_voice_note_id=None),),
        todos=(ExtractedTodo(body="Email Alice", source_voice_note_id="captures-recB"),),
    )
    block = render_dispatch_summary_block(payload)
    assert "### Decisions extracted" in block
    assert "Renew Client A" in block
    assert "[from captures-recA]" in block
    assert "### Wins extracted" in block
    assert "Closed ClientC" in block
    assert "### Todos surfaced (published to triage)" in block
    assert "Email Alice" in block
    assert "[from captures-recB]" in block


def test_render_summary_block_omits_empty_sections():
    payload = ReflectExtractionPayload(
        commentary="x",
        decisions=(ExtractedDecision(title="Only this"),),
    )
    block = render_dispatch_summary_block(payload)
    assert "### Decisions extracted" in block
    # No Wins / Todos headers when those arrays are empty.
    assert "### Wins extracted" not in block
    assert "### Todos surfaced" not in block
