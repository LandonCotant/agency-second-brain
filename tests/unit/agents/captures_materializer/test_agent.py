"""Unit tests for ``CapturesMaterializerAgent.invoke`` + ``run_tick``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.captures_materializer.agent import (
    AGENT_ID,
    CapturesMaterializerAgent,
    run_tick,
)
from agency_brain.agents.captures_materializer.airtable_writer import (
    CapturesAirtableWriter,
)
from agency_brain.agents.captures_materializer.dispatch import DispatchConfig
from agency_brain.agents.captures_materializer.models import (
    Capture,
    CaptureKind,
    CaptureScope,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBQ:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.calls.append((table_ref, rows))
        return []


class _FakeQuery:
    def query_rows(self, sql, parameters=None):
        return []


class _FakeFuture:
    def result(self, timeout: int = 30) -> str:
        return "msg-1"


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(self, topic, data, ordering_key="", **attrs):
        self.calls.append({"topic": topic, "ordering_key": ordering_key})
        return _FakeFuture()


class _FakeEmbedder:
    def embed(self, *, text: str, model: str) -> list[float]:
        return [0.1] * 768


class _FakeAirtableTable:
    def __init__(
        self,
        *,
        update_raises: Exception | None = None,
        delete_raises: Exception | None = None,
    ) -> None:
        self.updates: list[tuple[str, dict]] = []
        self.deletes: list[str] = []
        self._update_raises = update_raises
        self._delete_raises = delete_raises

    def update(self, record_id: str, fields: dict) -> dict:
        if self._update_raises:
            raise self._update_raises
        self.updates.append((record_id, fields))
        return {}

    def delete(self, record_id: str) -> dict:
        if self._delete_raises:
            raise self._delete_raises
        self.deletes.append(record_id)
        return {}


class _FakeAuditLog:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


def _config() -> DispatchConfig:
    return DispatchConfig(
        project_id="p",
        notes_table_ref="p.agent_outputs.notes",
        decisions_table_ref="p.agent_outputs.decisions",
        wins_table_ref="p.agent_outputs.wins",
        triage_topic_path="projects/p/topics/asb-triage-input",
    )


def _capture(kind: CaptureKind = CaptureKind.NOTE) -> Capture:
    return Capture(
        record_id="rec1",
        captured_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        note_text="hello",
        kind=kind,
        scope=CaptureScope.PERSONAL,
    )


def _agent(
    *,
    table: _FakeAirtableTable | None = None,
    bq: _FakeBQ | None = None,
    bq_query: _FakeQuery | None = None,
    publisher: _FakePublisher | None = None,
    audit: _FakeAuditLog | None = None,
) -> tuple[CapturesMaterializerAgent, dict[str, Any]]:
    table = table or _FakeAirtableTable()
    bq = bq or _FakeBQ()
    bq_query = bq_query or _FakeQuery()
    publisher = publisher or _FakePublisher()
    audit = audit or _FakeAuditLog()
    writer = CapturesAirtableWriter(table=table)
    agent = CapturesMaterializerAgent(
        sa_email="asb-x@p.iam.gserviceaccount.com",
        audit_log=audit,
        airtable_writer=writer,
        config=_config(),
        bq=bq,
        bq_query=bq_query,
        publisher=publisher,
        embedder=_FakeEmbedder(),
    )
    return agent, {
        "table": table,
        "bq": bq,
        "publisher": publisher,
        "audit": audit,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_invoke_note_writes_bq_publishes_flips_and_deletes() -> None:
    agent, fakes = _agent()
    outcome = agent.invoke(_capture(CaptureKind.NOTE))

    assert outcome.error is None
    assert outcome.bq_written is True
    assert outcome.triage_published is True

    # Airtable flip + delete
    assert len(fakes["table"].updates) == 1
    assert fakes["table"].updates[0][1]["Synced"] is True
    assert fakes["table"].deletes == ["rec1"]

    # One audit row, success=True, output reflects the full chain
    assert len(fakes["audit"].events) == 1
    ev = fakes["audit"].events[0]
    assert ev.success is True
    assert ev.error is None
    assert ev.agent_id == AGENT_ID
    import json

    output = json.loads(ev.output)
    assert output["flip_synced"] is True
    assert output["deleted"] is True
    assert output["target_table"] == "notes"


def test_invoke_todo_publishes_only_no_bq_write() -> None:
    agent, fakes = _agent()
    outcome = agent.invoke(_capture(CaptureKind.TODO))
    assert outcome.bq_written is False
    assert outcome.triage_published is True
    assert fakes["bq"].calls == []  # no BQ writes for todo
    assert fakes["table"].deletes == ["rec1"]  # but still flipped + deleted


# ---------------------------------------------------------------------------
# Failure modes per ADR 0039 §3
# ---------------------------------------------------------------------------


def test_invoke_dispatch_failure_skips_flip_and_delete() -> None:
    """Dispatch failure → no flip, no delete, audit row records the error."""

    class _BoomBQ(_FakeBQ):
        def insert_rows_json(self, table_ref, rows):
            return [{"index": 0, "errors": [{"reason": "schema"}]}]

    agent, fakes = _agent(bq=_BoomBQ())
    outcome = agent.invoke(_capture(CaptureKind.NOTE))

    assert outcome.error is not None
    assert fakes["table"].updates == []
    assert fakes["table"].deletes == []
    ev = fakes["audit"].events[0]
    assert ev.success is False
    assert ev.error is not None


def test_invoke_flip_synced_failure_skips_delete() -> None:
    table = _FakeAirtableTable(update_raises=RuntimeError("403"))
    agent, fakes = _agent(table=table)
    outcome = agent.invoke(_capture(CaptureKind.NOTE))

    assert outcome.error is not None
    assert "Captures.update failed" in outcome.error
    assert fakes["table"].deletes == []
    # The audit event records the failure — output has flip_synced=False
    import json

    output = json.loads(fakes["audit"].events[0].output)
    assert output["flip_synced"] is False
    assert output["deleted"] is False


def test_invoke_delete_failure_keeps_outcome_with_error() -> None:
    """Flip succeeds, delete fails — outcome carries the error so future
    debugging can find it; flip already marked the row Synced=TRUE so
    the next tick filters it out."""
    table = _FakeAirtableTable(delete_raises=RuntimeError("404"))
    agent, fakes = _agent(table=table)
    outcome = agent.invoke(_capture(CaptureKind.NOTE))

    assert outcome.error is not None
    assert "Captures.delete failed" in outcome.error
    assert fakes["table"].updates  # flip happened
    import json

    output = json.loads(fakes["audit"].events[0].output)
    assert output["flip_synced"] is True
    assert output["deleted"] is False


# ---------------------------------------------------------------------------
# run_tick aggregation
# ---------------------------------------------------------------------------


def test_run_tick_summarizes_per_row_outcomes() -> None:
    agent, _ = _agent()
    captures = [
        _capture(CaptureKind.NOTE),
        Capture(
            record_id="rec2",
            captured_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
            note_text="win",
            kind=CaptureKind.WIN,
            scope=CaptureScope.PERSONAL,
        ),
        Capture(
            record_id="rec3",
            captured_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
            note_text="todo",
            kind=CaptureKind.TODO,
            scope=CaptureScope.PERSONAL,
        ),
    ]
    summary = run_tick(captures, agent=agent)
    assert summary.listed == 3
    assert summary.materialized == 3
    # only note + todo publish
    assert summary.triage_published == 2
    assert summary.deleted == 3
    assert summary.failures == 0


def test_run_tick_continues_past_per_row_failure() -> None:
    """One bad row mustn't abort the tick — explicit per-row try/except."""

    class _SometimesBQ(_FakeBQ):
        def __init__(self):
            super().__init__()
            self.n = 0

        def insert_rows_json(self, table_ref, rows):
            self.n += 1
            if self.n == 1:
                return [{"index": 0, "errors": [{"reason": "schema"}]}]
            return super().insert_rows_json(table_ref, rows)

    agent, fakes = _agent(bq=_SometimesBQ())
    summary = run_tick(
        [
            _capture(CaptureKind.NOTE),
            Capture(
                record_id="rec2",
                captured_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
                note_text="hi",
                kind=CaptureKind.WIN,
                scope=CaptureScope.PERSONAL,
            ),
        ],
        agent=agent,
    )
    assert summary.listed == 2
    assert summary.materialized == 1
    assert summary.failures == 1
