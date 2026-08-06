"""Unit tests for the Gmail-into-corpus side-effect writer (ADR 0049).

Covers the load-bearing behaviors:

1. Idempotency: an email already in the corpus dedup-skips on the next
   processing pass (a re-run after the ``secondbrain-processed`` label
   is missing must not double-insert).
2. Markdown shape: header lines (From / Subject / Date / To) precede
   the body so the embedding picks up sender + subject keywords.
3. Embed failure is non-fatal: the row still lands without an
   embedding, so the corpus stays consistent even on transient Vertex
   blips.
4. BQ insert failure surfaces as ``inserted=False`` + a skip_reason —
   the caller never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from agency_brain.agents.crm_updater.models import GmailMessage
from agency_brain.agents.crm_updater.notes_writer import CrmNotesWriter


@dataclass
class _FakeBQRows:
    inserted: list[tuple[str, list[dict]]] = field(default_factory=list)
    reject_with: list | None = None

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.inserted.append((table_ref, rows))
        return list(self.reject_with) if self.reject_with else []


@dataclass
class _FakeBQQuery:
    existing_for: set[str] = field(default_factory=set)
    captured: list[tuple[str, list[dict] | None]] = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.captured.append((sql, parameters))
        if not parameters:
            return []
        msg_id = next((p["value"] for p in parameters if p["name"] == "msg_id"), None)
        if msg_id in self.existing_for:
            return [{"note_id": f"email:{msg_id}"}]
        return []


@dataclass
class _FakeEmbedder:
    raises: BaseException | None = None
    captured: list[tuple[str, str]] = field(default_factory=list)

    def embed(self, *, text: str, model: str) -> list[float]:
        self.captured.append((text, model))
        if self.raises is not None:
            raise self.raises
        return [0.0] * 768


def _message(
    *,
    message_id: str = "msg-1",
    subject: str = "Project kickoff",
    from_addr: str = "alice@example.com",
    body: str = "Confirming the kickoff next Tuesday at 10am PT.",
    to_addrs: tuple[str, ...] = ("owner@example.com",),
    received_at: datetime | None = None,
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id="thread-1",
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=(),
        body_text=body,
        received_at=received_at or datetime(2026, 5, 11, 14, 0, tzinfo=UTC),
    )


def _writer(
    *,
    existing: set[str] | None = None,
    embed_raises=None,
    insert_rejects=None,
) -> tuple[CrmNotesWriter, _FakeBQRows, _FakeBQQuery, _FakeEmbedder]:
    rows = _FakeBQRows(reject_with=insert_rejects)
    query = _FakeBQQuery(existing_for=existing or set())
    embedder = _FakeEmbedder(raises=embed_raises)
    writer = CrmNotesWriter(
        bq_rows=rows,
        bq_query=query,
        embedder=embedder,
        project_id="agency-brain-demo",
    )
    return writer, rows, query, embedder


# ---------------------------------------------------------------------------
# Happy path + markdown shape
# ---------------------------------------------------------------------------


def test_write_inserts_row_with_email_kind_and_agency_scope() -> None:
    writer, rows, _query, _embedder = _writer()

    outcome = writer.write(_message())

    assert outcome.inserted is True
    assert outcome.note_id == "email:msg-1"
    assert len(rows.inserted) == 1
    table_ref, row_batch = rows.inserted[0]
    assert table_ref == "agency-brain-demo.agent_outputs.notes"
    assert len(row_batch) == 1
    row = row_batch[0]
    assert row["note_kind"] == "email"
    assert row["scope"] == "agency"
    assert row["external_id"] == "msg-1"
    assert row["hipaa_isolated"] is False
    assert row["extraction_method"] == "email-passthrough"
    assert row["filename"] == "Project kickoff"
    assert row["note_id"] == "email:msg-1"
    assert row["revision_id"] == "msg-1"


def test_markdown_starts_with_header_lines() -> None:
    writer, rows, _q, _e = _writer()

    writer.write(_message(subject="Q3 lead-gen results", from_addr="bob@acme.com"))

    row = rows.inserted[0][1][0]
    md = row["markdown_content"]
    first_block = md.split("\n\n", 1)[0]
    assert first_block.startswith("From: bob@acme.com\n")
    assert "Subject: Q3 lead-gen results" in first_block
    assert "Date: 2026-05-11" in first_block
    # Body is preserved after the blank line
    assert "Confirming the kickoff next Tuesday" in md


def test_embedding_uses_headered_text_not_raw_body() -> None:
    writer, _r, _q, embedder = _writer()

    writer.write(_message(subject="ROAS update", from_addr="bob@acme.com"))

    embedded_text, _model = embedder.captured[0]
    # Embedded payload includes the header so /ask can match on
    # sender/subject keywords semantically.
    assert embedded_text.startswith("From: bob@acme.com\n")
    assert "Subject: ROAS update" in embedded_text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_dedup_skips_when_existing_row_present() -> None:
    """ADR 0049 §2 — a re-processed thread must not double-insert."""
    writer, rows, query, embedder = _writer(existing={"msg-1"})

    outcome = writer.write(_message())

    assert outcome.inserted is False
    assert outcome.skip_reason == "dedup_existing_row"
    assert rows.inserted == []
    assert embedder.captured == []
    # Dedup query must be parameterized + filter on note_kind='email'.
    sql, params = query.captured[0]
    assert "external_id = @msg_id" in sql
    assert "note_kind = 'email'" in sql
    assert {"name": "msg_id", "type": "STRING", "value": "msg-1"} in (params or [])


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_embed_failure_lands_row_without_embedding() -> None:
    """A Vertex blip must not drop the corpus row — landing without an
    embedding keeps the row available for backfill via the existing
    embeddings_only ingester mode (ADR 0039 §4)."""
    writer, rows, _q, _e = _writer(embed_raises=RuntimeError("vertex 5xx"))

    outcome = writer.write(_message())

    assert outcome.inserted is True
    row = rows.inserted[0][1][0]
    # No embedding columns set → row passes the
    # `ARRAY_LENGTH(embedding) = 768` filter as NULL.
    assert "embedding" not in row
    assert "embedding_model" not in row


def test_bq_insert_rejected_surfaces_skip_reason() -> None:
    writer, _r, _q, _e = _writer(insert_rejects=[{"reason": "bad row"}])

    outcome = writer.write(_message())

    assert outcome.inserted is False
    assert outcome.skip_reason is not None
    assert "bq_insert_rejected" in outcome.skip_reason


def test_dedup_query_failure_falls_back_to_insert() -> None:
    """If the dedup SELECT raises, the writer proceeds as if no row
    exists. Worst case: a duplicate row lands (rare; cheap to delete);
    not worse than dropping the corpus write entirely."""

    class _RaisingQuery:
        def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
            raise RuntimeError("transient")

    embedder = _FakeEmbedder()
    rows = _FakeBQRows()
    writer = CrmNotesWriter(
        bq_rows=rows,
        bq_query=_RaisingQuery(),  # type: ignore[arg-type]
        embedder=embedder,
        project_id="agency-brain-demo",
    )

    outcome = writer.write(_message())

    assert outcome.inserted is True
