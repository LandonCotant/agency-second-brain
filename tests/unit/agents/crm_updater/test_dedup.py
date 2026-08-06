"""Tests for ``RunCheckpointStore`` — read most-recent + write checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.crm_updater.dedup import (
    RunCheckpointStore,
    new_run_id,
)
from agency_brain.agents.crm_updater.models import RunCheckpoint


class _FakeBQQuery:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.last_sql: str | None = None

    def query_rows(self, sql, parameters=None):
        self.last_sql = sql
        return list(self.rows)


class _FakeBQWriter:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict]]] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]):
        self.inserted.append((table_ref, list(rows)))
        return []


def _store(rows: list[dict]) -> tuple[RunCheckpointStore, _FakeBQQuery, _FakeBQWriter]:
    q = _FakeBQQuery(rows)
    w = _FakeBQWriter()
    return RunCheckpointStore(bq_query=q, bq_writer=w, project_id="p"), q, w


def test_new_run_id_starts_with_prefix() -> None:
    rid = new_run_id()
    assert rid.startswith("crmu-")
    assert len(rid) > len("crmu-")


def test_latest_returns_none_when_no_rows() -> None:
    store, _, _ = _store(rows=[])
    assert store.latest_successful_run() is None


def test_latest_parses_row() -> None:
    iso = "2026-05-09T10:00:00+00:00"
    store, q, _ = _store(
        rows=[
            {
                "run_id": "crmu-aaaa",
                "started_at": iso,
                "ended_at": iso,
                "history_id_after": "12345",
                "messages_processed": 7,
                "drafts_created": 12,
                "errors": 0,
                "success": True,
            }
        ]
    )
    cp = store.latest_successful_run()
    assert cp is not None
    assert cp.run_id == "crmu-aaaa"
    assert cp.history_id_after == "12345"
    assert cp.messages_processed == 7
    assert cp.drafts_created == 12
    assert cp.errors == 0
    assert cp.success is True
    # SQL must filter to success-only.
    assert q.last_sql is not None
    assert "WHERE success = TRUE" in q.last_sql


def test_write_serializes_checkpoint() -> None:
    started = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC)
    ended = datetime(2026, 5, 9, 10, 0, 30, tzinfo=UTC)
    cp = RunCheckpoint(
        run_id="crmu-bbbb",
        started_at=started,
        ended_at=ended,
        history_id_after="99999",
        messages_processed=3,
        drafts_created=5,
        errors=0,
        success=True,
    )
    store, _, w = _store(rows=[])
    store.write(cp)
    assert len(w.inserted) == 1
    table_ref, rows = w.inserted[0]
    assert table_ref == "p.agent_outputs.crm_updater_runs"
    row = rows[0]
    assert row["run_id"] == "crmu-bbbb"
    assert row["history_id_after"] == "99999"
    assert row["success"] is True
    assert row["started_at"] == "2026-05-09T10:00:00+00:00"


def test_write_swallows_failures() -> None:
    """Failing to write the checkpoint must not crash the run — the next
    run will just re-process recent emails (drafts_dedup is the safety
    net via the secondbrain-processed label)."""
    started = datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC)
    cp = RunCheckpoint(
        run_id="crmu-cccc",
        started_at=started,
        ended_at=started,
        history_id_after=None,
        messages_processed=0,
        drafts_created=0,
        errors=0,
        success=True,
    )

    class _Boom:
        def insert_rows_json(self, table_ref, rows):
            raise RuntimeError("BQ down")

    store = RunCheckpointStore(
        bq_query=_FakeBQQuery([]),
        bq_writer=_Boom(),
        project_id="p",
    )
    # No raise.
    store.write(cp)
