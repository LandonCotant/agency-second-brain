"""Unit tests for ``RiskFlagsWriter`` — INSERT + same-day dedup."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from agency_brain.agents.risk_watcher.writer import (
    RiskFlagsWriteError,
    RiskFlagsWriter,
)


@dataclass
class _FakeRows:
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)
    errors: list = field(default_factory=list)

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.captured.append((table_ref, rows))
        return list(self.errors)


@dataclass
class _FakeQuery:
    canned: list[list[dict]] = field(default_factory=list)
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.captured.append((sql, list(parameters or [])))
        if self.canned:
            return self.canned.pop(0)
        return []


def _row(**overrides) -> dict:
    base = dict(
        flag_id="new-flag-1",
        flagged_at="2026-05-04T12:00:00+00:00",
        agent_run_id="run-1",
        account_id="recAcct1",
        project_id=None,
        segment="E-commerce",
        pattern_name="GapDetected",
        severity="high",
        signal_evidence="3 emails unanswered",
        data_sources=["airtable_replica.tasks"],
        baseline_snapshot=None,
        confidence=0.9,
        human_review_routed=False,
        reasoning="...",
        airtable_task_record_id=None,
        resolved_at=None,
        resolution_note=None,
        model="gemini-2.5-flash",
        prompt_version="v1",
    )
    base.update(overrides)
    return base


def test_insert_when_no_dedup_hit_returns_written_true() -> None:
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])  # no rows = no dedup hit
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    result = writer.write(_row())

    assert result.written is True
    assert result.flag_id == "new-flag-1"
    assert len(rows.captured) == 1
    assert rows.captured[0][0] == "agency-brain-demo.agent_outputs.risk_flags"


def test_dedup_hit_skips_insert_and_returns_existing_flag_id() -> None:
    rows = _FakeRows()
    # Two queries now run on the no-suppression path: the suppression
    # pre-check (ADR 0060, returns [] = no active mute) then the dedup
    # pre-check (returns the existing flag).
    query = _FakeQuery(canned=[[], [{"flag_id": "existing-flag-7"}]])
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    result = writer.write(_row())

    assert result.written is False
    assert result.suppressed is False
    assert result.flag_id == "existing-flag-7"
    assert rows.captured == []  # no insert


def test_dedup_query_keys_on_account_pattern_and_calendar_day() -> None:
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    writer.write(_row())

    # query.captured[0] is the suppression pre-check; [1] is the dedup.
    assert len(query.captured) == 2
    sql, params = query.captured[1]
    assert "account_id = @account_id" in sql
    assert "pattern_name = @pattern_name" in sql
    assert "DATE(flagged_at) = DATE(@flagged_at)" in sql
    assert "resolved_at IS NULL" in sql
    param_names = {p["name"] for p in params}
    assert param_names == {"account_id", "pattern_name", "flagged_at"}


def test_resolved_flag_does_not_suppress_new_insert() -> None:
    """The dedup pre-check filters on ``resolved_at IS NULL``, so a manually
    retired flag does not block a fresh signal on the same calendar day.

    The fake query client returns ``[]`` regardless of canned rows when the
    real BQ query would have filtered them out — to exercise that path here
    we just assert the SQL contains the filter (covered by the test above)
    and that the writer proceeds with INSERT given an empty result set.
    """
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])  # empty result set = no active match
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    result = writer.write(_row(flag_id="fresh-after-resolve"))

    assert result.written is True
    assert result.flag_id == "fresh-after-resolve"
    assert len(rows.captured) == 1


def test_no_query_client_skips_dedup() -> None:
    """In test/dev with no query client, dedup is bypassed."""
    rows = _FakeRows()
    writer = RiskFlagsWriter(rows_client=rows, query_client=None, project_id="agency-brain-demo")

    first = writer.write(_row(flag_id="a"))
    second = writer.write(_row(flag_id="b"))

    assert first.written is True
    assert second.written is True
    assert len(rows.captured) == 2


def test_bq_errors_raise_write_error() -> None:
    rows = _FakeRows(errors=[{"index": 0, "errors": [{"reason": "bad"}]}])
    query = _FakeQuery(canned=[[]])
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    with pytest.raises(RiskFlagsWriteError):
        writer.write(_row())


def test_table_ref_uses_default_dataset_and_table() -> None:
    rows = _FakeRows()
    writer = RiskFlagsWriter(rows_client=rows, query_client=None, project_id="agency-brain-demo")
    assert writer.table_ref == "agency-brain-demo.agent_outputs.risk_flags"


# ----------------------------- suppression gate (ADR 0060 §3) ----------------


def test_active_suppression_writes_flag_pre_resolved() -> None:
    """An active 'noise' verdict → the flag is INSERTed pre-resolved
    (resolved_at set + resolution_note referencing the feedback id), not
    dropped, so it stays in the audit trail but is invisible to the brief
    and the fan-out (both filter resolved_at IS NULL)."""
    rows = _FakeRows()
    query = _FakeQuery(canned=[[{"feedback_id": "fb-abc123def456"}]])
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    result = writer.write(_row(flag_id="muted-flag"))

    assert result.written is True
    assert result.suppressed is True
    assert result.suppressed_by == "fb-abc123def456"
    assert result.flag_id == "muted-flag"
    # The suppressed path skips the dedup query entirely (it could never
    # collapse a pre-resolved row), so only the suppression pre-check ran.
    assert len(query.captured) == 1
    # Exactly one row inserted, and it carries the pre-resolved markers.
    assert len(rows.captured) == 1
    inserted = rows.captured[0][1][0]
    assert inserted["resolved_at"] == inserted["flagged_at"]
    assert inserted["resolution_note"] == "auto-suppressed: feedback fb-abc123def456"


def test_suppression_query_filters_scope_verdict_pattern_and_mute() -> None:
    rows = _FakeRows()
    query = _FakeQuery(canned=[[]])  # no active mute
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    writer.write(_row())

    sql, params = query.captured[0]
    assert "signal_feedback" in sql
    assert "scope = 'risk'" in sql
    assert "verdict = 'noise'" in sql
    assert "account_id = @account_id" in sql
    # pattern-specific OR account-wide (pattern_name IS NULL) mute.
    assert "pattern_name = @pattern_name OR pattern_name IS NULL" in sql
    # time-box honored.
    assert "mute_until IS NULL OR mute_until > CURRENT_TIMESTAMP()" in sql
    param_names = {p["name"] for p in params}
    assert param_names == {"account_id", "pattern_name"}


def test_no_suppression_falls_through_to_dedup_then_insert() -> None:
    rows = _FakeRows()
    # suppression check empty, dedup check empty → normal insert.
    query = _FakeQuery(canned=[[], []])
    writer = RiskFlagsWriter(rows_client=rows, query_client=query, project_id="agency-brain-demo")

    result = writer.write(_row(flag_id="normal-flag"))

    assert result.written is True
    assert result.suppressed is False
    assert result.suppressed_by is None
    assert len(query.captured) == 2  # suppression + dedup
    assert len(rows.captured) == 1
    # The normally-written row keeps resolved_at NULL.
    assert rows.captured[0][1][0]["resolved_at"] is None


def test_no_query_client_skips_suppression_and_dedup() -> None:
    """In test/dev with no query client, both the suppression gate and the
    dedup pre-check are bypassed — the flag inserts normally."""
    rows = _FakeRows()
    writer = RiskFlagsWriter(rows_client=rows, query_client=None, project_id="agency-brain-demo")

    result = writer.write(_row(flag_id="a"))

    assert result.written is True
    assert result.suppressed is False
    assert len(rows.captured) == 1
    assert rows.captured[0][1][0]["resolved_at"] is None


def test_feedback_table_ref_uses_default() -> None:
    rows = _FakeRows()
    writer = RiskFlagsWriter(rows_client=rows, query_client=None, project_id="agency-brain-demo")
    assert writer.feedback_table_ref == "agency-brain-demo.agent_outputs.signal_feedback"
