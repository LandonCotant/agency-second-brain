"""Unit tests for the Evening Reflection writer + dedup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest
from agency_brain.agents.evening_reflection.models import EveningReflectionOutput
from agency_brain.agents.evening_reflection.writer import (
    EveningReflectionWriteError,
    EveningReflectionWriter,
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
    """Map (recipient, local_date_iso) -> reflection_id, or
    (recipient, local_date_iso, mode) -> reflection_id for ADR 0040 §7
    mode-aware dedup. Looks up the more specific key first."""

    def find_existing_reflection_id(
        self,
        table_ref: str,
        recipient_email: str,
        local_date: date,
        mode: str = "reflect",
    ) -> str | None:
        triple = (recipient_email, local_date.isoformat(), mode)
        if triple in self.existing:
            return self.existing[triple]
        return self.existing.get((recipient_email, local_date.isoformat()))


def _output(**overrides) -> EveningReflectionOutput:
    base = dict(
        reflection_id="r-1",
        recipient_email="owner@example.com",
        local_date=date(2026, 5, 5),
        body_markdown="### What happened today\n\n...",
        sections_used=("calendar",),
        prompt_version="v1",
    )
    base.update(overrides)
    return EveningReflectionOutput(**base)


def test_find_existing_returns_none_without_dedup_client():
    bq = _FakeBQRows()
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
    assert writer.find_existing("a@b.com", date(2026, 5, 5)) is None


def test_find_existing_hits_dedup_client():
    bq = _FakeBQRows()
    dedup = _FakeDedup(existing={("a@b.com", "2026-05-05"): "existing-1"})
    writer = EveningReflectionWriter(bq_client=bq, project_id="p", dedup_client=dedup)
    assert writer.find_existing("a@b.com", date(2026, 5, 5)) == "existing-1"
    assert writer.find_existing("a@b.com", date(2026, 5, 6)) is None


def test_write_inserts_one_row_with_correct_shape():
    bq = _FakeBQRows()
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
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
    assert table_ref == "p.agent_outputs.evening_reflections"
    assert len(rows) == 1
    row = rows[0]
    assert row["reflection_id"] == "r-1"
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
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
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
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
    with pytest.raises(EveningReflectionWriteError):
        writer.write(
            output=_output(),
            run_id="run-x",
            prompt_version="v1",
            model="gemini-2.5-flash",
            latency_ms=1,
            success=True,
        )


def test_write_carries_dedup_existing_id():
    bq = _FakeBQRows()
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
    out = _output(
        dedup_skipped=True,
        dedup_existing_reflection_id="existing-7",
        gmail_draft_id=None,
    )
    writer.write(
        output=out,
        run_id="run-x",
        prompt_version="v1",
        model="gemini-2.5-flash",
        latency_ms=1,
        success=True,
    )
    row = bq.captured[0][1][0]
    assert row["dedup_skipped"] is True
    assert row["dedup_existing_reflection_id"] == "existing-7"


# ------------------------------------------------- ADR 0040 §7 mode-aware dedup


def test_write_default_mode_is_reflect():
    """Backward compat: callers that don't pass mode produce mode='reflect' rows."""
    bq = _FakeBQRows()
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
    writer.write(
        output=_output(),
        run_id="run-x",
        prompt_version="v1",
        model="gemini-2.5-flash",
        latency_ms=1,
        success=True,
    )
    assert bq.captured[0][1][0]["mode"] == "reflect"


def test_write_carries_explicit_prompt_mode():
    bq = _FakeBQRows()
    writer = EveningReflectionWriter(bq_client=bq, project_id="p")
    writer.write(
        output=_output(),
        run_id="run-x",
        prompt_version="prompt_v1",
        model="gemini-2.5-flash",
        latency_ms=1,
        success=True,
        mode="prompt",
    )
    row = bq.captured[0][1][0]
    assert row["mode"] == "prompt"
    assert row["prompt_version"] == "prompt_v1"


def test_find_existing_returns_per_mode_match():
    """Mode-aware dedup: same date but different mode does NOT collide."""
    bq = _FakeBQRows()
    dedup = _FakeDedup(
        existing={
            ("a@b.com", "2026-05-05", "prompt"): "anchor-row-id",
            ("a@b.com", "2026-05-05", "reflect"): "reflection-row-id",
        }
    )
    writer = EveningReflectionWriter(bq_client=bq, project_id="p", dedup_client=dedup)
    assert writer.find_existing("a@b.com", date(2026, 5, 5), mode="prompt") == "anchor-row-id"
    assert writer.find_existing("a@b.com", date(2026, 5, 5), mode="reflect") == "reflection-row-id"


def test_find_existing_falls_back_to_unkeyed_for_pre_pr_c_rows():
    """Pre-PR-C rows have NULL mode; the ADR 0040 §7 dedup query treats
    them as 'reflect'. The fake's two-key fallback mirrors that."""
    bq = _FakeBQRows()
    dedup = _FakeDedup(existing={("a@b.com", "2026-05-05"): "pre-pr-c-id"})
    writer = EveningReflectionWriter(bq_client=bq, project_id="p", dedup_client=dedup)
    # Default mode='reflect' lookup hits the unkeyed pre-PR-C row.
    assert writer.find_existing("a@b.com", date(2026, 5, 5)) == "pre-pr-c-id"
