"""Unit tests for ``dispatch.py``. One test per Kind path + row builders."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.captures_materializer.dispatch import (
    DispatchConfig,
    build_decision_row,
    build_note_row,
    build_note_triage_envelope,
    build_todo_triage_envelope,
    build_win_row,
    dispatch,
)
from agency_brain.agents.captures_materializer.models import (
    Capture,
    CaptureKind,
    CaptureScope,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBQ:
    def __init__(self, errors: list | None = None) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self._errors = errors or []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.calls.append((table_ref, rows))
        return self._errors


class _FakeQuery:
    """Pre-INSERT SELECT dedup helper. Default: empty (no dedup hit)."""

    def __init__(self, *, hits: bool = False) -> None:
        self.calls: list[tuple[str, list[dict] | None]] = []
        self._hits = hits

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.calls.append((sql, parameters))
        return [{"_": 1}] if self._hits else []


class _FakeFuture:
    def __init__(self, message_id: str = "msg-1") -> None:
        self._id = message_id

    def result(self, timeout: int = 30) -> str:
        return self._id


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str = "",
        **attributes: str,
    ) -> Any:
        self.calls.append(
            {
                "topic": topic,
                "data": data,
                "ordering_key": ordering_key,
                "attributes": attributes,
            }
        )
        return _FakeFuture()


class _FakeEmbedder:
    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else [0.1] * 768
        self.calls: list[dict] = []

    def embed(self, *, text: str, model: str) -> list[float]:
        self.calls.append({"text": text, "model": model})
        return list(self._vector)


def _config() -> DispatchConfig:
    return DispatchConfig(
        project_id="test-proj",
        notes_table_ref="test-proj.agent_outputs.notes",
        decisions_table_ref="test-proj.agent_outputs.decisions",
        wins_table_ref="test-proj.agent_outputs.wins",
        triage_topic_path="projects/test-proj/topics/asb-triage-input",
        embedding_model="text-embedding-005",
    )


def _capture(
    *,
    kind: CaptureKind,
    scope: CaptureScope = CaptureScope.PERSONAL,
    note_text: str = "test capture",
    record_id: str = "recABC123",
    captured_at: datetime | None = None,
) -> Capture:
    return Capture(
        record_id=record_id,
        captured_at=captured_at or datetime(2026, 5, 6, 14, 30, tzinfo=UTC),
        note_text=note_text,
        kind=kind,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Kind=note path
# ---------------------------------------------------------------------------


def test_dispatch_note_writes_notes_row_and_publishes() -> None:
    bq = _FakeBQ()
    publisher = _FakePublisher()
    embedder = _FakeEmbedder()

    outcome = dispatch(
        _capture(kind=CaptureKind.NOTE, note_text="Phone call with Acme"),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=publisher,
        embedder=embedder,
    )

    assert outcome.bq_written is True
    assert outcome.triage_published is True
    assert outcome.target_table == "notes"
    assert outcome.error is None

    # BQ insert
    assert len(bq.calls) == 1
    table_ref, rows = bq.calls[0]
    assert table_ref == "test-proj.agent_outputs.notes"
    assert len(rows) == 1
    row = rows[0]
    assert row["note_id"] == "captures-recABC123"
    assert row["note_kind"] == "inbox"
    assert row["scope"] == "personal"
    assert row["extraction_method"] == "markdown-passthrough"
    assert row["markdown_content"] == "Phone call with Acme"
    assert len(row["embedding"]) == 768
    assert row["embedding_model"] == "text-embedding-005"

    # Pub/Sub publish
    assert len(publisher.calls) == 1
    pub = publisher.calls[0]
    assert pub["topic"] == "projects/test-proj/topics/asb-triage-input"
    assert pub["ordering_key"] == "captures-recABC123"
    envelope = json.loads(pub["data"].decode("utf-8"))
    assert envelope["source"] == "drive"
    assert envelope["source_event_ref"] == "captures/recABC123"
    assert envelope["aspects"] == ["captures_note"]

    # Embedder called with body
    assert len(embedder.calls) == 1
    assert embedder.calls[0]["text"] == "Phone call with Acme"


def test_dispatch_note_agency_scope() -> None:
    """Scope hint propagates to the BQ row."""
    bq = _FakeBQ()
    outcome = dispatch(
        _capture(kind=CaptureKind.NOTE, scope=CaptureScope.AGENCY),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    assert outcome.scope is CaptureScope.AGENCY
    _, rows = bq.calls[0]
    assert rows[0]["scope"] == "agency"


def test_dispatch_note_empty_body_skips_embed() -> None:
    """Empty body → no embedding call → embedding_model NULL on the row."""
    bq = _FakeBQ()
    embedder = _FakeEmbedder()
    dispatch(
        _capture(kind=CaptureKind.NOTE, note_text="   "),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=embedder,
    )
    _, rows = bq.calls[0]
    assert rows[0]["embedding"] == []
    assert rows[0]["embedding_model"] is None
    assert embedder.calls == []


def test_dispatch_note_bq_failure_returns_error_no_publish() -> None:
    """BQ errors abort the dispatch — Pub/Sub MUST NOT publish (else the
    next tick re-publishes after re-INSERT)."""
    bq = _FakeBQ(errors=[{"index": 0, "errors": [{"reason": "schema"}]}])
    publisher = _FakePublisher()

    outcome = dispatch(
        _capture(kind=CaptureKind.NOTE),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=publisher,
        embedder=_FakeEmbedder(),
    )

    assert outcome.bq_written is False
    assert outcome.triage_published is False
    assert outcome.error is not None
    assert "BQ rejected" in outcome.error
    assert publisher.calls == []


# ---------------------------------------------------------------------------
# Kind=decision path
# ---------------------------------------------------------------------------


def test_dispatch_decision_writes_decisions_row() -> None:
    bq = _FakeBQ()
    captured = datetime(2026, 5, 6, 9, 0, tzinfo=UTC)
    outcome = dispatch(
        _capture(
            kind=CaptureKind.DECISION,
            note_text="Hire Sarah for the brand role.\nShe interviewed best.",
            captured_at=captured,
        ),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )

    assert outcome.bq_written is True
    assert outcome.triage_published is False
    assert outcome.target_table == "decisions"

    table_ref, rows = bq.calls[0]
    assert table_ref == "test-proj.agent_outputs.decisions"
    row = rows[0]
    assert row["title"] == "Hire Sarah for the brand role."
    assert row["status"] == "draft"
    assert row["decided_at"] == "2026-05-06T09:00:00+00:00"
    assert row["review_30_at"] == "2026-06-05"
    assert row["review_90_at"] == "2026-08-04"
    assert row["review_365_at"] == "2027-05-06"
    assert row["alternatives"] == []
    assert row["prediction"] is None
    assert row["confidence"] is None


def test_dispatch_decision_truncates_long_first_line() -> None:
    bq = _FakeBQ()
    long_first = "A" * 200 + "\nrest"
    dispatch(
        _capture(kind=CaptureKind.DECISION, note_text=long_first),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    row = bq.calls[0][1][0]
    # Truncated to 80 with ellipsis
    assert len(row["title"]) <= 80
    assert row["title"].endswith("…")


# ---------------------------------------------------------------------------
# Kind=win path
# ---------------------------------------------------------------------------


def test_dispatch_win_writes_wins_row_with_iso_monday() -> None:
    bq = _FakeBQ()
    # Wednesday May 6, 2026 → ISO Monday is May 4, 2026
    captured = datetime(2026, 5, 6, 14, 0, tzinfo=UTC)
    outcome = dispatch(
        _capture(
            kind=CaptureKind.WIN,
            note_text="Closed Acme contract Q2",
            captured_at=captured,
        ),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )

    assert outcome.bq_written is True
    assert outcome.target_table == "wins"
    table_ref, rows = bq.calls[0]
    assert table_ref == "test-proj.agent_outputs.wins"
    row = rows[0]
    assert row["title"] == "Closed Acme contract Q2"
    assert row["source_kind"] == "manual"
    assert row["source_id"] == "recABC123"
    assert row["week_of"] == "2026-05-04"
    assert row["evidence_links"] == []


def test_dispatch_win_monday_capture_stays_on_monday() -> None:
    """A Monday capture's week_of equals the capture date itself."""
    bq = _FakeBQ()
    monday = datetime(2026, 5, 4, 8, 0, tzinfo=UTC)
    dispatch(
        _capture(kind=CaptureKind.WIN, captured_at=monday),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    assert bq.calls[0][1][0]["week_of"] == "2026-05-04"


def test_dispatch_win_sunday_capture_uses_prior_monday() -> None:
    """ISO week starts Monday — Sunday belongs to the same Monday-led week."""
    bq = _FakeBQ()
    sunday = datetime(2026, 5, 10, 23, 0, tzinfo=UTC)  # Sunday
    dispatch(
        _capture(kind=CaptureKind.WIN, captured_at=sunday),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    # ISO Monday of the week containing Sun May 10 = Mon May 4
    assert bq.calls[0][1][0]["week_of"] == "2026-05-04"


# ---------------------------------------------------------------------------
# Kind=todo path
# ---------------------------------------------------------------------------


def test_dispatch_todo_publishes_only_no_bq() -> None:
    bq = _FakeBQ()
    publisher = _FakePublisher()

    outcome = dispatch(
        _capture(kind=CaptureKind.TODO, note_text="Email Sam about Q3 plan"),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=publisher,
        embedder=_FakeEmbedder(),
    )

    assert outcome.bq_written is False
    assert outcome.triage_published is True
    assert outcome.target_table is None
    assert bq.calls == []
    assert len(publisher.calls) == 1

    pub = publisher.calls[0]
    assert pub["ordering_key"] == "captures/recABC123"
    envelope = json.loads(pub["data"].decode("utf-8"))
    assert envelope["source"] == "airtable"
    assert envelope["source_event_ref"] == "captures/recABC123"
    assert envelope["aspects"] == ["captures_todo"]
    assert envelope["body"] == "Email Sam about Q3 plan"


def test_dispatch_todo_publish_failure_propagates() -> None:
    """A publish exception lands in outcome.error so the row stays Synced=FALSE."""

    class _BoomPublisher:
        def publish(self, *args, **kwargs):
            raise RuntimeError("topic does not exist")

    outcome = dispatch(
        _capture(kind=CaptureKind.TODO),
        config=_config(),
        bq=_FakeBQ(),
        bq_query=_FakeQuery(),
        publisher=_BoomPublisher(),
        embedder=_FakeEmbedder(),
    )
    assert outcome.triage_published is False
    assert outcome.error is not None
    assert "topic does not exist" in outcome.error


# ---------------------------------------------------------------------------
# Row-builder direct tests
# ---------------------------------------------------------------------------


def test_build_note_row_deterministic_dedup_key() -> None:
    """note_id is fully derived from record_id — same input twice = same key."""
    cap = _capture(kind=CaptureKind.NOTE, record_id="recXXX")
    embedder = _FakeEmbedder()
    now = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    row1 = build_note_row(cap, embedder=embedder, embedding_model="m", now=now)
    row2 = build_note_row(cap, embedder=embedder, embedding_model="m", now=now)
    assert row1["note_id"] == row2["note_id"] == "captures-recXXX"


def test_build_note_row_content_hash_is_sha256_of_body() -> None:
    """Idempotency contract per ADR 0038 §4."""
    import hashlib

    cap = _capture(kind=CaptureKind.NOTE, note_text="hello world")
    row = build_note_row(
        cap,
        embedder=_FakeEmbedder(),
        embedding_model="m",
        now=datetime.now(UTC),
    )
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert row["embedding_content_hash"] == expected


def test_build_decision_row_review_dates_align_to_captured_date() -> None:
    cap = _capture(
        kind=CaptureKind.DECISION,
        captured_at=datetime(2026, 1, 31, 0, 0, tzinfo=UTC),
    )
    row = build_decision_row(cap, now=datetime.now(UTC))
    # 30/90/365 days from 2026-01-31
    assert row["review_30_at"] == "2026-03-02"
    assert row["review_90_at"] == "2026-05-01"
    assert row["review_365_at"] == "2027-01-31"


def test_build_win_row_iso_monday_for_thursday() -> None:
    cap = _capture(
        kind=CaptureKind.WIN,
        captured_at=datetime(2026, 5, 7, 16, 0, tzinfo=UTC),  # Thursday
    )
    row = build_win_row(cap, now=datetime.now(UTC))
    assert row["week_of"] == "2026-05-04"  # Mon of that week


def test_build_note_triage_envelope_shape() -> None:
    cap = _capture(kind=CaptureKind.NOTE, note_text="line one\nline two")
    env = build_note_triage_envelope(
        cap,
        sender_email="owner@example.com",
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
    )
    assert env["source"] == "drive"
    assert env["source_event_ref"] == "captures/recABC123"
    assert env["subject"] == "line one"
    assert env["body"] == "line one\nline two"
    assert env["aspects"] == ["captures_note"]


def test_build_todo_triage_envelope_uses_airtable_source() -> None:
    cap = _capture(kind=CaptureKind.TODO, note_text="todo body")
    env = build_todo_triage_envelope(
        cap,
        sender_email="owner@example.com",
        now=datetime.now(UTC),
    )
    assert env["source"] == "airtable"
    assert env["aspects"] == ["captures_todo"]


# ---------------------------------------------------------------------------
# Pre-INSERT dedup (ADR 0039 §3)
# ---------------------------------------------------------------------------


def test_dispatch_note_dedup_hit_skips_insert_still_publishes() -> None:
    """Dedup hit on an existing note row → no second INSERT, but still
    publish + return success so the orchestrator runs the flip+delete
    that the previous tick must have failed at."""
    bq = _FakeBQ()
    publisher = _FakePublisher()
    outcome = dispatch(
        _capture(kind=CaptureKind.NOTE),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(hits=True),
        publisher=publisher,
        embedder=_FakeEmbedder(),
    )
    assert outcome.bq_written is False
    assert outcome.triage_published is True
    assert outcome.error is None
    assert bq.calls == []
    assert len(publisher.calls) == 1


def test_dispatch_decision_dedup_hit_skips_insert() -> None:
    bq = _FakeBQ()
    outcome = dispatch(
        _capture(kind=CaptureKind.DECISION),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(hits=True),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    assert outcome.bq_written is False
    assert outcome.error is None
    assert bq.calls == []


def test_dispatch_win_dedup_hit_skips_insert() -> None:
    bq = _FakeBQ()
    outcome = dispatch(
        _capture(kind=CaptureKind.WIN),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(hits=True),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    assert outcome.bq_written is False
    assert outcome.error is None
    assert bq.calls == []


def test_dispatch_decision_uses_deterministic_id() -> None:
    bq = _FakeBQ()
    dispatch(
        _capture(kind=CaptureKind.DECISION, record_id="recXYZ"),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    row = bq.calls[0][1][0]
    assert row["decision_id"] == "captures-decision-recXYZ"


def test_dispatch_win_uses_deterministic_id() -> None:
    bq = _FakeBQ()
    dispatch(
        _capture(kind=CaptureKind.WIN, record_id="recXYZ"),
        config=_config(),
        bq=bq,
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    row = bq.calls[0][1][0]
    assert row["win_id"] == "captures-win-recXYZ"


# ---------------------------------------------------------------------------
# Unmapped kind safety
# ---------------------------------------------------------------------------


def test_dispatch_rejects_unmapped_kind() -> None:
    """A new CaptureKind value without a dispatcher must surface as an error
    on the outcome — not a silent BQ write to the wrong table."""

    class _FakeKind:
        value = "rogue"

        def __repr__(self) -> str:
            return "FakeKind.ROGUE"

    cap = Capture(
        record_id="rec1",
        captured_at=datetime.now(UTC),
        note_text="x",
        kind=_FakeKind(),  # type: ignore[arg-type]
        scope=CaptureScope.PERSONAL,
    )
    outcome = dispatch(
        cap,
        config=_config(),
        bq=_FakeBQ(),
        bq_query=_FakeQuery(),
        publisher=_FakePublisher(),
        embedder=_FakeEmbedder(),
    )
    assert outcome.error is not None
    assert "unmapped" in outcome.error.lower()
