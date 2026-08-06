"""Unit tests for the Captures Airtable mutator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agency_brain.agents.captures_materializer.airtable_writer import (
    AirtableWriteError,
    CapturesAirtableWriter,
)


class _FakeTable:
    def __init__(
        self,
        *,
        update_raises: Exception | None = None,
        delete_raises: Exception | None = None,
    ) -> None:
        self.update_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self._update_raises = update_raises
        self._delete_raises = delete_raises

    def update(self, record_id: str, fields: dict) -> dict:
        if self._update_raises is not None:
            raise self._update_raises
        self.update_calls.append((record_id, fields))
        return {"id": record_id, "fields": fields}

    def delete(self, record_id: str) -> dict:
        if self._delete_raises is not None:
            raise self._delete_raises
        self.delete_calls.append(record_id)
        return {"deleted": True, "id": record_id}


def test_flip_synced_sets_synced_true_and_date() -> None:
    table = _FakeTable()
    w = CapturesAirtableWriter(table=table)
    w.flip_synced("recABC", now=datetime(2026, 5, 6, 14, 0, tzinfo=UTC))
    assert table.update_calls == [
        ("recABC", {"Synced": True, "Synced At": "2026-05-06"}),
    ]


def test_flip_synced_default_now() -> None:
    """Without explicit ``now=``, the writer stamps today's UTC date."""
    table = _FakeTable()
    w = CapturesAirtableWriter(table=table)
    w.flip_synced("recABC")
    assert len(table.update_calls) == 1
    record_id, fields = table.update_calls[0]
    assert record_id == "recABC"
    assert fields["Synced"] is True
    # YYYY-MM-DD shape
    assert len(fields["Synced At"]) == 10
    assert fields["Synced At"][4] == "-"


def test_flip_synced_wraps_pyairtable_errors() -> None:
    table = _FakeTable(update_raises=RuntimeError("403 Forbidden"))
    w = CapturesAirtableWriter(table=table)
    with pytest.raises(AirtableWriteError) as exc_info:
        w.flip_synced("recABC")
    assert "Captures.update failed for recABC" in str(exc_info.value)
    assert "403 Forbidden" in str(exc_info.value)


def test_delete_capture_calls_delete() -> None:
    table = _FakeTable()
    w = CapturesAirtableWriter(table=table)
    w.delete_capture("recABC")
    assert table.delete_calls == ["recABC"]


def test_delete_capture_wraps_errors() -> None:
    table = _FakeTable(delete_raises=RuntimeError("404 Not Found"))
    w = CapturesAirtableWriter(table=table)
    with pytest.raises(AirtableWriteError) as exc_info:
        w.delete_capture("recABC")
    assert "Captures.delete failed for recABC" in str(exc_info.value)
