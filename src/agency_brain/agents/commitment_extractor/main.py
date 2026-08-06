"""Cloud Run Job entrypoint for the commitment extractor (ADR 0069).

One execution per scheduler tick:
  1. Read the watermark cursor (max ``ingested_at`` processed last run).
  2. Scan ``agent_outputs.notes`` for new, non-HIPAA, in-scope notes not
     already represented in ``commitments``.
  3. Per note: Gemini-extract commitments, drop below the confidence floor.
  4. Resolve ``account_id`` from counterparty email (one batched lookup).
  5. Insert rows + advance the watermark.

Drafts-only: writes solely to ``agent_outputs.commitments`` (ADR 0069 §6).

Required env:
  - BRAIN_PROJECT_ID
Optional env:
  - COMMITMENT_SOURCE_NOTE_KINDS (default "email,inbox,calendar_event,capture")
  - COMMITMENT_MIN_CONFIDENCE    (default 0.6)
  - COMMITMENT_STALE_DAYS        (default 7; used by readers, not the writer)
  - COMMITMENT_MAX_NOTES_PER_TICK (default 200)
  - BRAIN_VERTEX_LOCATION        (default us-central1)
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from .extractor import Extractor, ExtractorConfig
from .models import STATUS_OPEN, CommitmentRow, SourceNote

log = logging.getLogger("agency_brain.agents.commitment_extractor.main")

WATERMARK_CURSOR = "commitment-extractor"


class NotesScanner(Protocol):
    def scan(self, *, since: str | None, kinds: list[str], limit: int) -> list[SourceNote]: ...


class AccountResolver(Protocol):
    def resolve(self, emails: set[str]) -> dict[str, str]: ...


class CommitmentsWriter(Protocol):
    def insert(self, rows: list[CommitmentRow]) -> None: ...


class Watermark(Protocol):
    def read(self) -> str | None: ...
    def write(self, cursor: str) -> None: ...


def run_extraction(
    *,
    extractor: Extractor,
    scanner: NotesScanner,
    resolver: AccountResolver,
    writer: CommitmentsWriter,
    watermark: Watermark,
    source_kinds: list[str],
    min_confidence: float,
    max_notes: int,
    run_id: str,
    today: str,
    now_iso: str,
) -> dict[str, Any]:
    """Pure orchestration — all collaborators injected for testability."""
    cursor = watermark.read()
    notes = scanner.scan(since=cursor, kinds=source_kinds, limit=max_notes)

    extracted: list[tuple[SourceNote, Any]] = []
    emails: set[str] = set()
    cost = 0.0
    max_seen = cursor
    for note in notes:
        try:
            result = extractor.extract(note=note, today=today)
        except Exception:
            log.exception("commitment_extractor.extract_failed note_id=%s", note.note_id)
            continue
        cost += result.cost_usd
        for c in result.commitments:
            if c.confidence < min_confidence:
                continue
            extracted.append((note, c))
            if c.counterparty_email:
                emails.add(c.counterparty_email.lower())
        if max_seen is None or note.ingested_at > max_seen:
            max_seen = note.ingested_at

    account_map = resolver.resolve(emails) if emails else {}

    rows: list[CommitmentRow] = []
    for note, c in extracted:
        email_key = (c.counterparty_email or "").lower()
        rows.append(
            CommitmentRow(
                commitment_id=uuid.uuid4().hex,
                extracted_at=now_iso,
                source_note_id=note.note_id,
                source_note_kind=note.note_kind,
                direction=c.direction,
                counterparty_email=c.counterparty_email,
                counterparty_name=c.counterparty_name,
                account_id=account_map.get(email_key),
                commitment_text=c.commitment_text,
                due_date=c.due_date,
                status=STATUS_OPEN,
                confidence=c.confidence,
                reasoning=c.reasoning,
                agent_run_id=run_id,
            )
        )

    if rows:
        writer.insert(rows)
    if max_seen is not None and max_seen != cursor:
        watermark.write(max_seen)

    stats = {
        "scanned": len(notes),
        "commitments_written": len(rows),
        "cost_usd": round(cost, 6),
        "watermark_advanced": bool(max_seen and max_seen != cursor),
    }
    log.info("commitment_extractor.done %s", stats)
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    location = os.environ.get("BRAIN_VERTEX_LOCATION", "us-central1")
    source_kinds = [
        k.strip()
        for k in os.environ.get(
            "COMMITMENT_SOURCE_NOTE_KINDS", "email,inbox,calendar_event,capture"
        ).split(",")
        if k.strip()
    ]
    min_confidence = float(os.environ.get("COMMITMENT_MIN_CONFIDENCE", "0.6"))
    max_notes = int(os.environ.get("COMMITMENT_MAX_NOTES_PER_TICK", "200"))

    from google.cloud import bigquery

    bq = bigquery.Client(project=project_id)
    run_id = uuid.uuid4().hex
    now = datetime.now(tz=UTC)

    extractor = Extractor(ExtractorConfig(project_id=project_id, location=location))
    scanner = _BQNotesScanner(bq, project_id)
    resolver = _BQAccountResolver(bq, project_id)
    writer = _BQCommitmentsWriter(bq, project_id)
    watermark = _BQWatermark(bq, project_id)

    log.info(
        "commitment_extractor.start project=%s kinds=%s min_conf=%.2f run_id=%s",
        project_id,
        source_kinds,
        min_confidence,
        run_id,
    )
    try:
        run_extraction(
            extractor=extractor,
            scanner=scanner,
            resolver=resolver,
            writer=writer,
            watermark=watermark,
            source_kinds=source_kinds,
            min_confidence=min_confidence,
            max_notes=max_notes,
            run_id=run_id,
            today=now.date().isoformat(),
            now_iso=now.isoformat(),
        )
    except Exception:
        log.exception("commitment_extractor.run_failed")
        return 1
    return 0


# ----------------------------------------------------- BQ adapters


class _BQNotesScanner:
    """Scan new, non-HIPAA, in-scope notes not already in commitments."""

    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def scan(self, *, since: str | None, kinds: list[str], limit: int) -> list[SourceNote]:
        from google.cloud import bigquery

        where = [
            "ARRAY_LENGTH(embedding) = 768",
            "COALESCE(hipaa_isolated, FALSE) = FALSE",
            "note_kind IN UNNEST(@kinds)",
            "markdown_content IS NOT NULL",
            (
                f"note_id NOT IN (SELECT source_note_id FROM "  # noqa: S608
                f"`{self._p}.agent_outputs.commitments` "
                f"WHERE source_note_id IS NOT NULL)"
            ),
        ]
        params: list[Any] = [
            bigquery.ArrayQueryParameter("kinds", "STRING", kinds),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if since:
            where.append("ingested_at > @since")
            params.append(bigquery.ScalarQueryParameter("since", "TIMESTAMP", since))
        sql = (
            f"SELECT note_id, note_kind, markdown_content, ingested_at "  # noqa: S608
            f"FROM `{self._p}.agent_outputs.notes` "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY ingested_at ASC LIMIT @limit"
        )
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        out: list[SourceNote] = []
        for r in self._bq.query(sql, job_config=job_config).result():
            ts = r.get("ingested_at")
            out.append(
                SourceNote(
                    note_id=str(r.get("note_id") or ""),
                    note_kind=str(r.get("note_kind") or ""),
                    markdown_content=str(r.get("markdown_content") or ""),
                    ingested_at=ts.isoformat() if ts is not None else "",
                )
            )
        return out


class _BQAccountResolver:
    """Map counterparty email -> Airtable account rec id via contacts replica."""

    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def resolve(self, emails: set[str]) -> dict[str, str]:
        from google.cloud import bigquery

        # contacts.account is a linked-record array; the first element is the
        # primary account rec id (same derivation as person.py SAFE_OFFSET(0)).
        sql = (
            f"SELECT LOWER(email) AS email, "  # noqa: S608
            f"account[SAFE_OFFSET(0)] AS account_id "
            f"FROM `{self._p}.airtable_replica.contacts` "
            f"WHERE LOWER(email) IN UNNEST(@emails) "
            f"AND ARRAY_LENGTH(account) > 0"
        )
        params = [
            bigquery.ArrayQueryParameter("emails", "STRING", sorted(emails)),
        ]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        out: dict[str, str] = {}
        try:
            for r in self._bq.query(sql, job_config=job_config).result():
                email = r.get("email")
                acct = r.get("account_id")
                if email and acct:
                    out[str(email)] = str(acct)
        except Exception:
            log.exception("commitment_extractor.account_resolve_failed")
        return out


class _BQCommitmentsWriter:
    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def insert(self, rows: list[CommitmentRow]) -> None:
        table = f"{self._p}.agent_outputs.commitments"
        payload = [
            {
                "commitment_id": r.commitment_id,
                "extracted_at": r.extracted_at,
                "source_note_id": r.source_note_id,
                "source_note_kind": r.source_note_kind,
                "direction": r.direction,
                "counterparty_email": r.counterparty_email,
                "counterparty_name": r.counterparty_name,
                "account_id": r.account_id,
                "commitment_text": r.commitment_text,
                "due_date": r.due_date,
                "status": r.status,
                "confidence": r.confidence,
                "reasoning": r.reasoning,
                "agent_run_id": r.agent_run_id,
            }
            for r in rows
        ]
        errors = self._bq.insert_rows_json(table, payload)
        if errors:
            raise RuntimeError(f"commitments insert failed: {errors}")


class _BQWatermark:
    """Insert-only cursor in agent_state.commitment_extractor_watermark."""

    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def read(self) -> str | None:
        sql = (
            f"SELECT last_ingested_at_seen FROM "  # noqa: S608
            f"`{self._p}.agent_state.commitment_extractor_watermark` "
            f"WHERE cursor_name = '{WATERMARK_CURSOR}' "
            f"ORDER BY updated_at DESC LIMIT 1"
        )
        try:
            for r in self._bq.query(sql).result():
                ts = r.get("last_ingested_at_seen")
                return ts.isoformat() if ts is not None else None
        except Exception:
            log.exception("commitment_extractor.watermark_read_failed")
        return None

    def write(self, cursor: str) -> None:
        table = f"{self._p}.agent_state.commitment_extractor_watermark"
        errors = self._bq.insert_rows_json(
            table,
            [
                {
                    "cursor_name": WATERMARK_CURSOR,
                    "last_ingested_at_seen": cursor,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                }
            ],
        )
        if errors:
            raise RuntimeError(f"watermark write failed: {errors}")


if __name__ == "__main__":
    sys.exit(main())
