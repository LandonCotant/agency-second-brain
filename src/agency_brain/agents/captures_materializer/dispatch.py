"""Kind→action dispatch for the Captures materializer (ADR 0039 §2).

Each Capture row has a ``Kind`` selector that routes it to one of four
materializations:

- ``note``     → ``agent_outputs.notes`` (with inline embed) + Pub/Sub
- ``decision`` → ``agent_outputs.decisions`` (status='draft', 30/90/365 reviews)
- ``win``      → ``agent_outputs.wins`` (week_of = ISO Monday)
- ``todo``     → Pub/Sub publish only (Triage Agent produces its own row)

Pure-logic helpers here build the BQ row dicts + Pub/Sub envelopes from
a Capture; I/O is owned by ``readers``, ``airtable_writer``, and the BQ
clients constructed in ``main.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from .models import Capture, CaptureKind, MaterializeOutcome

# Note rows mirror the embedder ``EmbeddingResult`` shape from notes-ingestor
# so the materializer's inline embed produces the same column shape pre-merge
# Phase 1 RAG queries care about. Kept loose (no hard import) so this module
# stays embedder-agnostic and tests can pass a fake.


class Embedder(Protocol):
    """Mirrors ``notes_ingestor.embedder.Embedder`` — single embed call."""

    def embed(self, *, text: str, model: str) -> list[float]: ...


class BQRowsClient(Protocol):
    """Streaming-insert surface; matches ``insert_rows_json``."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Parameterized SELECT for the pre-INSERT dedup check (ADR 0039 §3)."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class PubSubPublisherClient(Protocol):
    """Matches ``google.cloud.pubsub_v1.PublisherClient.publish``."""

    def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str = "",
        **attributes: str,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchConfig:
    """Per-tick wiring the dispatcher needs. ``main.py`` constructs once."""

    project_id: str
    notes_table_ref: str
    decisions_table_ref: str
    wins_table_ref: str
    triage_topic_path: str
    embedding_model: str = "text-embedding-005"
    sender_email: str = "owner@example.com"


def dispatch(
    capture: Capture,
    *,
    config: DispatchConfig,
    bq: BQRowsClient,
    bq_query: BQQueryClient,
    publisher: PubSubPublisherClient,
    embedder: Embedder,
    now_utc: datetime | None = None,
) -> MaterializeOutcome:
    """Route ``capture`` to the right materialization per its Kind.

    Returns a ``MaterializeOutcome``. Raises nothing — failures land in
    ``outcome.error`` so the caller can decide whether to retry next tick
    (no Synced flip / no DELETE on error).
    """
    now = now_utc or datetime.now(UTC)
    try:
        if capture.kind is CaptureKind.NOTE:
            return _dispatch_note(
                capture,
                config=config,
                bq=bq,
                bq_query=bq_query,
                publisher=publisher,
                embedder=embedder,
                now=now,
            )
        if capture.kind is CaptureKind.DECISION:
            return _dispatch_decision(capture, config=config, bq=bq, bq_query=bq_query, now=now)
        if capture.kind is CaptureKind.WIN:
            return _dispatch_win(capture, config=config, bq=bq, bq_query=bq_query, now=now)
        if capture.kind is CaptureKind.TODO:
            return _dispatch_todo(capture, config=config, publisher=publisher)
        raise ValueError(f"unmapped capture kind: {capture.kind!r}")
    except Exception as exc:
        return MaterializeOutcome(
            record_id=capture.record_id,
            kind=capture.kind,
            scope=capture.scope,
            bq_written=False,
            triage_published=False,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Per-Kind dispatchers
# ---------------------------------------------------------------------------


def _dispatch_note(
    capture: Capture,
    *,
    config: DispatchConfig,
    bq: BQRowsClient,
    bq_query: BQQueryClient,
    publisher: PubSubPublisherClient,
    embedder: Embedder,
    now: datetime,
) -> MaterializeOutcome:
    note_id = note_id_for(capture.record_id)
    if _row_exists(
        bq_query,
        table_ref=config.notes_table_ref,
        column="note_id",
        value=note_id,
    ):
        # Pre-INSERT dedup hit (ADR 0039 §3). Skip the BQ write but still
        # fall through to publish + flip + delete — the previous tick's
        # flip-or-delete must have failed for us to be here.
        bq_written = False
    else:
        note_row = build_note_row(
            capture,
            embedder=embedder,
            embedding_model=config.embedding_model,
            now=now,
        )
        errors = bq.insert_rows_json(config.notes_table_ref, [note_row])
        if errors:
            raise RuntimeError(f"BQ rejected notes insert: {errors}")
        bq_written = True

    envelope = build_note_triage_envelope(capture, sender_email=config.sender_email, now=now)
    triage_published = _publish(
        publisher,
        topic=config.triage_topic_path,
        envelope=envelope,
        ordering_key=note_id,
    )

    return MaterializeOutcome(
        record_id=capture.record_id,
        kind=capture.kind,
        scope=capture.scope,
        bq_written=bq_written,
        triage_published=triage_published,
        target_table="notes",
    )


def _dispatch_decision(
    capture: Capture,
    *,
    config: DispatchConfig,
    bq: BQRowsClient,
    bq_query: BQQueryClient,
    now: datetime,
) -> MaterializeOutcome:
    decision_id = decision_id_for(capture.record_id)
    if _row_exists(
        bq_query,
        table_ref=config.decisions_table_ref,
        column="decision_id",
        value=decision_id,
    ):
        return MaterializeOutcome(
            record_id=capture.record_id,
            kind=capture.kind,
            scope=capture.scope,
            bq_written=False,
            triage_published=False,
            target_table="decisions",
        )
    row = build_decision_row(capture, now=now)
    errors = bq.insert_rows_json(config.decisions_table_ref, [row])
    if errors:
        raise RuntimeError(f"BQ rejected decisions insert: {errors}")
    return MaterializeOutcome(
        record_id=capture.record_id,
        kind=capture.kind,
        scope=capture.scope,
        bq_written=True,
        triage_published=False,
        target_table="decisions",
    )


def _dispatch_win(
    capture: Capture,
    *,
    config: DispatchConfig,
    bq: BQRowsClient,
    bq_query: BQQueryClient,
    now: datetime,
) -> MaterializeOutcome:
    win_id = win_id_for(capture.record_id)
    if _row_exists(
        bq_query,
        table_ref=config.wins_table_ref,
        column="win_id",
        value=win_id,
    ):
        return MaterializeOutcome(
            record_id=capture.record_id,
            kind=capture.kind,
            scope=capture.scope,
            bq_written=False,
            triage_published=False,
            target_table="wins",
        )
    row = build_win_row(capture, now=now)
    errors = bq.insert_rows_json(config.wins_table_ref, [row])
    if errors:
        raise RuntimeError(f"BQ rejected wins insert: {errors}")
    return MaterializeOutcome(
        record_id=capture.record_id,
        kind=capture.kind,
        scope=capture.scope,
        bq_written=True,
        triage_published=False,
        target_table="wins",
    )


def _dispatch_todo(
    capture: Capture,
    *,
    config: DispatchConfig,
    publisher: PubSubPublisherClient,
) -> MaterializeOutcome:
    envelope = build_todo_triage_envelope(
        capture,
        sender_email=config.sender_email,
        now=datetime.now(UTC),
    )
    triage_published = _publish(
        publisher,
        topic=config.triage_topic_path,
        envelope=envelope,
        ordering_key=f"captures/{capture.record_id}",
    )
    return MaterializeOutcome(
        record_id=capture.record_id,
        kind=capture.kind,
        scope=capture.scope,
        bq_written=False,
        triage_published=triage_published,
        target_table=None,
    )


# ---------------------------------------------------------------------------
# Row builders — exposed for unit tests
# ---------------------------------------------------------------------------


def note_id_for(record_id: str) -> str:
    """Deterministic ``agent_outputs.notes.note_id`` for a Captures record.

    ADR 0039 §2 — ``captures-{airtable_record_id}`` so re-runs are idempotent
    against a pre-INSERT SELECT.
    """
    return f"captures-{record_id}"


def decision_id_for(record_id: str) -> str:
    """Deterministic ``agent_outputs.decisions.decision_id`` for a Captures
    record. Aligned with ``note_id_for`` so the dedup contract is uniform
    across kinds (ADR 0039 §3 — pre-INSERT SELECT skip)."""
    return f"captures-decision-{record_id}"


def win_id_for(record_id: str) -> str:
    """Deterministic ``agent_outputs.wins.win_id`` for a Captures record."""
    return f"captures-win-{record_id}"


def build_note_row(
    capture: Capture,
    *,
    embedder: Embedder,
    embedding_model: str,
    now: datetime,
) -> dict:
    """Build the ``agent_outputs.notes`` row dict for a Kind=note capture.

    Embedding is inline (ADR 0039 §6) so a single materialize call lands
    a fully-embedded row. Mirrors the column shape the notes-ingestor
    writer produces (see ``notes_ingestor.models.NoteRow.to_bq_row``).
    """
    import hashlib

    note_id = note_id_for(capture.record_id)
    body = capture.note_text or ""
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    vector = embedder.embed(text=body, model=embedding_model) if body.strip() else []

    return {
        "note_id": note_id,
        "revision_id": "captures-v1",
        "ingested_at": now.isoformat(),
        "created_at": capture.captured_at.isoformat(),
        "source_drive_file_id": "",
        "source_drive_url": "",
        "filename": _filename_from_body(body),
        "markdown_content": body,
        "extraction_method": "markdown-passthrough",
        "extraction_confidence": 1.0,
        "extraction_notes": None,
        "page_count": 1,
        "hipaa_isolated": False,
        "triaged_item_id": None,
        # ADR 0037 / 0038 PKM merge fields
        "note_kind": "inbox",
        "scope": capture.scope.value,
        "embedding_content_hash": content_hash,
        "embedding": list(vector) if vector else [],
        "embedding_model": embedding_model if vector else None,
        "embedding_generated_at": now.isoformat() if vector else None,
    }


def build_decision_row(capture: Capture, *, now: datetime) -> dict:
    """Build the ``agent_outputs.decisions`` row for a Kind=decision capture.

    Per ADR 0039 §2, draft-status row with title = first-line truncated;
    review_30/90/365 dates derive from ``Captured At``. The user fills in
    alternatives + prediction + confidence later via the (future)
    ``/decide --refine`` analog or a BQ console edit.

    Note: ``decision_id`` is deterministic (``captures-decision-{record_id}``)
    so the pre-INSERT SELECT in ``_dispatch_decision`` can dedup re-runs
    cleanly. ADR 0039 §2 specs ``uuid4()``; we use a deterministic key
    instead because ADR 0039 §3 mandates pre-INSERT SELECT idempotency,
    and a UUID per row would defeat that.
    """
    body = capture.note_text or ""
    title = _first_line_truncated(body, max_len=80)
    captured_date = capture.captured_at.date()
    return {
        "decision_id": decision_id_for(capture.record_id),
        "decided_at": capture.captured_at.isoformat(),
        "title": title,
        "context": body,
        "alternatives": [],
        "choice": body,
        "prediction": None,
        "confidence": None,
        "review_30_at": (captured_date + timedelta(days=30)).isoformat(),
        "review_90_at": (captured_date + timedelta(days=90)).isoformat(),
        "review_365_at": (captured_date + timedelta(days=365)).isoformat(),
        "status": "draft",
        "source_reflection_id": None,
        "source_voice_note_id": None,
        "refined_at": None,
        "retrospective_30": None,
        "retrospective_90": None,
        "retrospective_365": None,
        "calibration_score": None,
        "agent_run_id": None,
    }


def build_win_row(capture: Capture, *, now: datetime) -> dict:
    """Build the ``agent_outputs.wins`` row for a Kind=win capture.

    ``win_id`` is deterministic for the same idempotency reason as
    ``decision_id`` (see ``build_decision_row``).
    """
    body = capture.note_text or ""
    title = _first_line_truncated(body, max_len=80)
    captured_date = capture.captured_at.date()
    return {
        "win_id": win_id_for(capture.record_id),
        "captured_at": capture.captured_at.isoformat(),
        "week_of": _monday_of_iso_week(captured_date).isoformat(),
        "source_kind": "manual",
        "source_id": capture.record_id,
        "title": title,
        "summary": body,
        "evidence_links": [],
        "agent_run_id": None,
    }


def build_note_triage_envelope(capture: Capture, *, sender_email: str, now: datetime) -> dict:
    """Pub/Sub envelope shape mirroring notes-ingestor's triage publish.

    ``source = 'drive'`` is intentional — the triage bridge / classifier
    treats this as the same content kind as Drive-ingested notes (ADR
    0037 §6 publishes only ``inbox``-kind content), so reusing the Drive
    envelope means no bridge changes.
    """
    return {
        "source": "drive",
        "source_url": "",
        "source_event_ref": f"captures/{capture.record_id}",
        "sender": sender_email,
        "subject": _first_line_truncated(capture.note_text or "", max_len=120),
        "body": capture.note_text or "",
        "ingested_at": now.isoformat(),
        "aspects": ["captures_note"],
    }


def build_todo_triage_envelope(capture: Capture, *, sender_email: str, now: datetime) -> dict:
    """Pub/Sub envelope for Kind=todo. ADR 0039 §2 — `source='airtable'`
    so a future query can reconcile manual todos against the
    ``triaged_items`` row Triage produces."""
    return {
        "source": "airtable",
        "source_url": "",
        "source_event_ref": f"captures/{capture.record_id}",
        "sender": sender_email,
        "subject": _first_line_truncated(capture.note_text or "", max_len=80),
        "body": capture.note_text or "",
        "ingested_at": now.isoformat(),
        "aspects": ["captures_todo"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _publish(
    publisher: PubSubPublisherClient,
    *,
    topic: str,
    envelope: dict,
    ordering_key: str,
) -> bool:
    data = json.dumps(envelope).encode("utf-8")
    future = publisher.publish(topic, data, ordering_key=ordering_key)
    if hasattr(future, "result"):
        future.result(timeout=30)
    return True


def _row_exists(
    bq_query: BQQueryClient,
    *,
    table_ref: str,
    column: str,
    value: str,
) -> bool:
    """Pre-INSERT dedup SELECT (ADR 0039 §3).

    The column whitelist is ``note_id``/``decision_id``/``win_id``; the
    table_ref is constructed by ``main.py`` from BRAIN_PROJECT_ID + the
    fixed dataset/table names. The column is interpolated literally
    (limited to known values), and the value is parameterized — no
    user-controlled SQL surface.
    """
    if column not in {"note_id", "decision_id", "win_id"}:
        raise ValueError(f"refusing to dedup on unknown column: {column!r}")
    sql = (
        f"SELECT 1 FROM `{table_ref}` "  # noqa: S608 — table_ref + column whitelisted
        f"WHERE {column} = @value LIMIT 1"
    )
    rows = bq_query.query_rows(
        sql,
        parameters=[{"name": "value", "type": "STRING", "value": value}],
    )
    return bool(rows)


def _first_line_truncated(text: str, *, max_len: int) -> str:
    first = (text.splitlines() or [""])[0].strip()
    if not first:
        return "(empty capture)"
    if len(first) <= max_len:
        return first
    return first[: max_len - 1].rstrip() + "…"


def _filename_from_body(text: str) -> str:
    """Synthetic filename for ``agent_outputs.notes`` — captures aren't
    files, but the column is REQUIRED so we surface the first line."""
    first = _first_line_truncated(text, max_len=60)
    return f"capture: {first}"


def _monday_of_iso_week(d: date) -> date:
    """Return the Monday of the ISO week containing ``d``. ``date.weekday()``
    returns 0 for Monday, so subtracting that many days lands on Monday."""
    return d - timedelta(days=d.weekday())
