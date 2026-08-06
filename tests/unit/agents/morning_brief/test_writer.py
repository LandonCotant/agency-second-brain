"""Unit tests for the Morning Brief writer + dedup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest
from agency_brain.agents.morning_brief.models import MorningBriefOutput
from agency_brain.agents.morning_brief.writer import (
    MorningBriefWriteError,
    MorningBriefWriter,
)


@dataclass
class _FakeBQRows:
    captured: list[tuple[str, list[dict]]] = field(default_factory=list)
    errors_to_return: list = field(default_factory=list)

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.captured.append((table_ref, rows))
        return list(self.errors_to_return)


@dataclass
class _FakeDedup:
    existing: dict = field(default_factory=dict)
    """Map (recipient, local_date_iso) -> brief_id when a row exists."""

    def find_existing_brief_id(
        self, table_ref: str, recipient_email: str, local_date: date
    ) -> str | None:
        return self.existing.get((recipient_email, local_date.isoformat()))


def _output(**overrides) -> MorningBriefOutput:
    base = dict(
        brief_id="b-1",
        recipient_email="owner@example.com",
        local_date=date(2026, 5, 5),
        body_markdown="# Brief",
        sections_used=("calendar",),
        prompt_version="v1",
    )
    base.update(overrides)
    return MorningBriefOutput(**base)


def test_find_existing_returns_none_without_dedup_client():
    bq = _FakeBQRows()
    writer = MorningBriefWriter(bq_client=bq, project_id="p")
    assert writer.find_existing("a@b.com", date(2026, 5, 5)) is None


def test_find_existing_hits_dedup_client():
    bq = _FakeBQRows()
    dedup = _FakeDedup(existing={("a@b.com", "2026-05-05"): "existing-1"})
    writer = MorningBriefWriter(bq_client=bq, project_id="p", dedup_client=dedup)
    assert writer.find_existing("a@b.com", date(2026, 5, 5)) == "existing-1"
    assert writer.find_existing("a@b.com", date(2026, 5, 6)) is None


def test_write_inserts_one_row_with_correct_shape():
    bq = _FakeBQRows()
    writer = MorningBriefWriter(bq_client=bq, project_id="p")
    out = _output(gmail_draft_id="r-99")
    writer.write(
        output=out,
        run_id="run-x",
        prompt_version="v1",
        model="gemini-2.5-flash",
        latency_ms=1234,
        success=True,
    )
    assert len(bq.captured) == 1
    table_ref, rows = bq.captured[0]
    assert table_ref == "p.agent_outputs.morning_briefs"
    assert len(rows) == 1
    row = rows[0]
    assert row["brief_id"] == "b-1"
    assert row["recipient_email"] == "owner@example.com"
    assert row["local_date"] == "2026-05-05"
    assert row["sections_used"] == ["calendar"]
    assert row["gmail_draft_id"] == "r-99"
    assert row["dedup_skipped"] is False
    assert row["latency_ms"] == 1234
    assert row["success"] is True
    assert row["error"] is None


def test_write_carries_failure_details():
    bq = _FakeBQRows()
    writer = MorningBriefWriter(bq_client=bq, project_id="p")
    out = _output(body_markdown="(failed before composition)")
    writer.write(
        output=out,
        run_id="run-x",
        prompt_version="v1",
        model="gemini-2.5-flash",
        latency_ms=42,
        success=False,
        error="RuntimeError: kaboom",
    )
    row = bq.captured[0][1][0]
    assert row["success"] is False
    assert row["error"] == "RuntimeError: kaboom"


def test_write_raises_when_bq_rejects():
    bq = _FakeBQRows(errors_to_return=[{"errors": [{"reason": "invalid"}]}])
    writer = MorningBriefWriter(bq_client=bq, project_id="p")
    with pytest.raises(MorningBriefWriteError):
        writer.write(
            output=_output(),
            run_id="run-x",
            prompt_version="v1",
            model="gemini-2.5-flash",
            latency_ms=1,
            success=True,
        )
