"""Unit tests for ADR 0040 §6 extracted_writers — idempotency keys,
row builders, and pre-INSERT dedup behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agency_brain.agents.evening_reflection.extracted_writers import (
    DecisionsWriter,
    WinsWriter,
    build_decision_row,
    build_win_row,
    decision_id_for,
    normalize_title,
    title_hash12,
    win_id_for,
)
from agency_brain.agents.evening_reflection.models import (
    ExtractedDecision,
    ExtractedWin,
)

# --------------------------------------------------- normalize / hash


def test_normalize_title_lowercases_and_collapses_whitespace():
    assert normalize_title("Hire Alice") == "hire alice"
    assert normalize_title("  HIRE   ALICE  ") == "hire alice"
    assert normalize_title("Hire\tAlice\n") == "hire alice"


def test_normalize_title_strips_punctuation():
    assert normalize_title("Hire Alice.") == "hire alice"
    assert normalize_title("Hire 'Alice'!") == "hire alice"
    assert normalize_title("Renew/Client A") == "renewclient a"


def test_normalize_title_truncates_to_64_chars():
    long_title = "a" * 100
    assert len(normalize_title(long_title)) == 64


def test_title_hash12_stable_across_trivial_variation():
    """LLM emitting 'Hire Alice' on tick A and 'Hire Alice.' on tick B
    must produce the same hash — that's the whole point of normalize."""
    a = title_hash12("Hire Alice")
    b = title_hash12("Hire Alice.")
    c = title_hash12("hire alice")
    d = title_hash12("HIRE ALICE")
    assert a == b == c == d
    assert len(a) == 12


def test_title_hash12_differs_for_different_titles():
    assert title_hash12("Hire Alice") != title_hash12("Hire Bob")


# --------------------------------------------------- ID construction


def test_decision_id_uses_voice_note_when_available():
    did = decision_id_for(
        reflection_id="r-123",
        voice_note_id="captures-recVOICE",
        title="Renew Client A",
    )
    assert did.startswith("reflection-captures-recVOICE-")


def test_decision_id_falls_back_to_reflection_id_when_no_voice_memo():
    did = decision_id_for(
        reflection_id="r-456",
        voice_note_id=None,
        title="Renew Client A",
    )
    assert did.startswith("reflection-r-456-")


def test_decision_id_stable_across_trivial_title_variation():
    a = decision_id_for(reflection_id="r-1", voice_note_id=None, title="Hire Alice")
    b = decision_id_for(reflection_id="r-1", voice_note_id=None, title="Hire Alice.")
    assert a == b


def test_win_id_mirrors_decision_id_format():
    """Same anchor logic + same hash, so dedup is uniform across kinds."""
    wid = win_id_for(
        reflection_id="r-1",
        voice_note_id="captures-recVOICE",
        title="Closed ClientC Q3",
    )
    assert wid.startswith("reflection-captures-recVOICE-")


# --------------------------------------------------- row builders


def test_build_decision_row_status_draft_with_30_90_365_dates():
    now = datetime(2026, 5, 6, 21, 0, tzinfo=UTC)
    ed = ExtractedDecision(
        title="Renew Client A",
        context="Q3 contract expires soon",
        source_voice_note_id="captures-recA",
    )
    row = build_decision_row(
        ed,
        reflection_id="r-1",
        decision_id="reflection-r-1-abc123",
        agent_run_id="run-99",
        now=now,
    )
    assert row["status"] == "draft"
    assert row["decision_id"] == "reflection-r-1-abc123"
    assert row["title"] == "Renew Client A"
    assert row["context"] == "Q3 contract expires soon"
    assert row["source_reflection_id"] == "r-1"
    assert row["source_voice_note_id"] == "captures-recA"
    assert row["agent_run_id"] == "run-99"
    # 30/90/365 dates relative to now.
    assert row["review_30_at"] == (now.date() + timedelta(days=30)).isoformat()
    assert row["review_90_at"] == (now.date() + timedelta(days=90)).isoformat()
    assert row["review_365_at"] == (now.date() + timedelta(days=365)).isoformat()
    # Empty alternatives + null prediction/confidence/retros (draft posture).
    assert row["alternatives"] == []
    assert row["prediction"] is None
    assert row["confidence"] is None
    assert row["retrospective_30"] is None


def test_build_decision_row_choice_falls_back_to_title_when_context_empty():
    ed = ExtractedDecision(title="X", context=None)
    row = build_decision_row(
        ed,
        reflection_id="r",
        decision_id="d",
        agent_run_id=None,
        now=datetime(2026, 5, 6, 21, 0, tzinfo=UTC),
    )
    # If no context, choice falls back to title (so the column is populated).
    assert row["choice"] == "X"


def test_build_win_row_source_kind_reflection():
    now = datetime(2026, 5, 6, 21, 0, tzinfo=UTC)  # Wednesday
    ew = ExtractedWin(
        title="Closed ClientC Q3", summary="Got the contract back", source_voice_note_id=None
    )
    row = build_win_row(
        ew,
        reflection_id="r-1",
        win_id="reflection-r-1-xyz",
        agent_run_id="run-99",
        now=now,
    )
    assert row["source_kind"] == "reflection"
    assert row["source_id"] == "r-1"
    assert row["title"] == "Closed ClientC Q3"
    assert row["summary"] == "Got the contract back"
    assert row["agent_run_id"] == "run-99"
    # Wednesday 2026-05-06 → Monday 2026-05-04
    assert row["week_of"] == "2026-05-04"
    assert row["evidence_links"] == []


# --------------------------------------------------- writers + dedup


@dataclass
class _FakeBQQuery:
    existing_values: set[str] = field(default_factory=set)
    last_sql: str = ""
    last_params: list = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        self.last_params = parameters or []
        # Simulate the `WHERE column = @value` pre-check.
        if not parameters:
            return []
        value = parameters[0]["value"]
        return [{"x": 1}] if value in self.existing_values else []


@dataclass
class _FakeBQRows:
    captured: list = field(default_factory=list)
    raise_on_insert: bool = False

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        if self.raise_on_insert:
            return [{"index": 0, "errors": [{"reason": "test"}]}]
        self.captured.append((table_ref, rows))
        return []


def test_decisions_writer_inserts_when_no_dedup_hit():
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery()
    writer = DecisionsWriter(
        bq=bq,
        bq_query=bq_query,
        table_ref="proj.agent_outputs.decisions",
    )
    ed = ExtractedDecision(
        title="Renew Client A", context="Q3", source_voice_note_id="captures-recA"
    )
    out = writer.write(ed, reflection_id="r-1", agent_run_id="run-99")
    assert out.written is True
    assert out.error is None
    assert out.target_id.startswith("reflection-captures-recA-")
    assert len(bq.captured) == 1
    table_ref, rows = bq.captured[0]
    assert table_ref == "proj.agent_outputs.decisions"
    assert rows[0]["title"] == "Renew Client A"
    # Dedup SELECT used parameterized value, not literal interpolation.
    assert "@value" in bq_query.last_sql
    assert bq_query.last_params[0]["value"] == out.target_id


def test_decisions_writer_skips_on_dedup_hit():
    expected_id = decision_id_for(
        reflection_id="r-1",
        voice_note_id="captures-recA",
        title="Renew Client A",
    )
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery(existing_values={expected_id})
    writer = DecisionsWriter(bq=bq, bq_query=bq_query, table_ref="proj.agent_outputs.decisions")
    ed = ExtractedDecision(
        title="Renew Client A", context="Q3", source_voice_note_id="captures-recA"
    )
    out = writer.write(ed, reflection_id="r-1", agent_run_id="run-99")
    assert out.written is False
    assert out.target_id == expected_id
    # Dedup hit means no INSERT call.
    assert bq.captured == []


def test_decisions_writer_returns_outcome_with_error_on_insert_failure():
    bq = _FakeBQRows(raise_on_insert=True)
    bq_query = _FakeBQQuery()
    writer = DecisionsWriter(bq=bq, bq_query=bq_query, table_ref="proj.agent_outputs.decisions")
    out = writer.write(
        ExtractedDecision(title="X", context=None),
        reflection_id="r-1",
        agent_run_id=None,
    )
    assert out.written is False
    assert out.error is not None
    assert "BQ rejected" in out.error


def test_wins_writer_idempotent_on_re_run():
    bq = _FakeBQRows()
    bq_query = _FakeBQQuery()
    writer = WinsWriter(bq=bq, bq_query=bq_query, table_ref="proj.agent_outputs.wins")
    ew = ExtractedWin(title="Closed ClientC Q3", summary="Got it", source_voice_note_id=None)
    out1 = writer.write(ew, reflection_id="r-1", agent_run_id=None)
    assert out1.written is True
    # Mark it as existing now (simulating second tick after first wrote).
    bq_query.existing_values.add(out1.target_id)
    out2 = writer.write(ew, reflection_id="r-1", agent_run_id=None)
    assert out2.written is False
    assert out2.target_id == out1.target_id
    # Only one INSERT call total.
    assert len(bq.captured) == 1
