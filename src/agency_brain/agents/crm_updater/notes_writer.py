"""Email-to-corpus side-effect writer (ADR 0049).

After the CRM Auto-updater extracts structured drafts from a
``secondbrain``-labeled email, this module ALSO writes a corpus row
to ``agent_outputs.notes`` so the email body becomes retrievable via
``/ask``. Email content rarely produces a Task but often holds the
context a future query needs ("what did Acme send last week?").

One row per email, keyed on ``external_id = message_id``,
``note_kind = 'email'``. Idempotent: a pre-INSERT SELECT checks for
the same key and skips if present. Embedding uses the same
``text-embedding-005`` + ``ARRAY_LENGTH = 768`` invariant the rest of
the corpus relies on (ADR 0038).

Markdown shape::

    From: alice@example.com
    Subject: Project kickoff next week
    Date: 2026-05-11T14:32:00+00:00

    <body_text>

The headered shape is what gets embedded so /ask retrieves emails by
sender / subject keywords.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..notes_ingestor.embedder import (
    DEFAULT_MODEL as DEFAULT_EMBED_MODEL,
)
from ..notes_ingestor.embedder import (
    EmbedError,
    embed_markdown,
)
from .models import GmailMessage

log = logging.getLogger("agency_brain.agents.crm_updater.notes_writer")


class BQRowsClient(Protocol):
    """Matches ``google.cloud.bigquery.Client.insert_rows_json``."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Parameterized SELECT helper (matches the crm_updater main adapter)."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class EmbedderProto(Protocol):
    def embed(self, *, text: str, model: str) -> list[float]: ...


@dataclass(frozen=True)
class NotesWriteOutcome:
    """Per-email outcome the agent records on the audit row."""

    note_id: str
    inserted: bool
    """True when a fresh row landed; False when dedup-skipped or embed-failed."""
    skip_reason: str | None = None


class CrmNotesWriter:
    """Insert one row into ``agent_outputs.notes`` per processed email.

    Idempotency: pre-INSERT SELECT on
    ``(external_id, note_kind='email')`` — same shape the Calendar
    ingester uses for ``(external_id, note_kind='calendar_event')`` so
    re-processing a thread does not double-write.

    Failure isolation: an embedder or BQ error logs + returns
    ``inserted=False`` but never raises into the caller. The CRM draft
    write (Tasks / Pending Updates) is the primary product of this
    Job; a failed corpus-write must not block draft creation.
    """

    _TABLE = "agent_outputs.notes"

    def __init__(
        self,
        *,
        bq_rows: BQRowsClient,
        bq_query: BQQueryClient,
        embedder: EmbedderProto,
        project_id: str,
        embedding_model: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self._rows = bq_rows
        self._query = bq_query
        self._embedder = embedder
        self._project_id = project_id
        self._embedding_model = embedding_model

    @property
    def table_ref(self) -> str:
        return f"{self._project_id}.{self._TABLE}"

    def write(self, message: GmailMessage) -> NotesWriteOutcome:
        """Idempotent INSERT for one Gmail message.

        Returns the outcome regardless of inserted-vs-skipped so the
        caller can record it on the agent's audit row.
        """
        if self._find_existing(message.message_id):
            return NotesWriteOutcome(
                note_id=_note_id_for(message.message_id),
                inserted=False,
                skip_reason="dedup_existing_row",
            )

        markdown = _format_email_markdown(message)
        try:
            embedding = embed_markdown(
                markdown=markdown,
                embedder=self._embedder,
                model=self._embedding_model,
            )
        except EmbedError:
            log.exception(
                "crm_updater.notes_writer.embed_failed message_id=%s",
                message.message_id,
            )
            embedding = None

        row = _to_bq_row(
            message=message,
            markdown=markdown,
            embedding_vector=tuple(embedding.vector) if embedding else (),
            embedding_model=(embedding.model if embedding and embedding.vector else None),
            embedding_hash=(embedding.content_hash if embedding else None),
        )
        errors = self._rows.insert_rows_json(self.table_ref, [row])
        if errors:
            log.error(
                "crm_updater.notes_writer.insert_failed message_id=%s errors=%s",
                message.message_id,
                errors,
            )
            return NotesWriteOutcome(
                note_id=row["note_id"],
                inserted=False,
                skip_reason=f"bq_insert_rejected: {errors}",
            )
        return NotesWriteOutcome(note_id=row["note_id"], inserted=True)

    def _find_existing(self, message_id: str) -> bool:
        sql = (
            f"SELECT note_id FROM `{self.table_ref}` "  # noqa: S608 — table_ref is internal
            "WHERE external_id = @msg_id AND note_kind = 'email' LIMIT 1"
        )
        try:
            rows = self._query.query_rows(
                sql,
                parameters=[{"name": "msg_id", "type": "STRING", "value": message_id}],
            )
        except Exception:
            log.exception(
                "crm_updater.notes_writer.dedup_query_failed message_id=%s — proceeding as if new",
                message_id,
            )
            return False
        return bool(rows)


# --------------------------------------------------------------------- helpers


def _format_email_markdown(message: GmailMessage) -> str:
    """ADR 0049 §3 — header lines precede the body so embeddings + /ask
    synthesis see the sender + subject + date alongside the content."""
    received = message.received_at.astimezone(UTC).isoformat()
    lines = [
        f"From: {message.from_addr}",
        f"Subject: {message.subject}",
        f"Date: {received}",
    ]
    if message.to_addrs:
        lines.append(f"To: {', '.join(message.to_addrs)}")
    header = "\n".join(lines)
    body = (message.body_text or "").strip()
    return f"{header}\n\n{body}"


def _note_id_for(message_id: str) -> str:
    """Stable, prefixed note_id so /ask + dashboards can tell at a
    glance that this row came from email, not Drive."""
    return f"email:{message_id}"


def _to_bq_row(
    *,
    message: GmailMessage,
    markdown: str,
    embedding_vector: tuple[float, ...],
    embedding_model: str | None,
    embedding_hash: str | None,
) -> dict[str, Any]:
    now_iso = datetime.now(UTC).isoformat()
    row: dict[str, Any] = {
        "note_id": _note_id_for(message.message_id),
        "revision_id": message.message_id,
        "ingested_at": now_iso,
        "created_at": message.received_at.astimezone(UTC).isoformat(),
        "source_drive_file_id": "",
        "source_drive_url": "",
        "filename": message.subject or "(no subject)",
        "markdown_content": markdown,
        "extraction_method": "email-passthrough",
        "extraction_confidence": 1.0,
        "page_count": 0,
        "hipaa_isolated": False,
        "note_kind": "email",
        "scope": "agency",
        "external_id": message.message_id,
    }
    if embedding_vector:
        row["embedding"] = list(embedding_vector)
        row["embedding_generated_at"] = now_iso
    if embedding_model is not None:
        row["embedding_model"] = embedding_model
    if embedding_hash is not None:
        row["embedding_content_hash"] = embedding_hash
    return row


__all__ = [
    "BQQueryClient",
    "BQRowsClient",
    "CrmNotesWriter",
    "EmbedderProto",
    "NotesWriteOutcome",
]
