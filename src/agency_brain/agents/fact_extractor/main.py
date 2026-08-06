"""Cloud Run Job entrypoint for the fact extractor (ADR 0070).

One execution per scheduler tick:
  1. Read the watermark cursor (max ``ingested_at`` processed last run).
  2. Scan ``agent_outputs.notes`` for new, non-HIPAA, in-scope notes not
     already represented in ``facts``.
  3. Per note: Gemini-extract entity-attribute facts, drop below the floor.
  4. Resolve ``entity_id``/``entity_type`` from name/email (batched lookup).
  5. Append rows + advance the watermark.

Append-only: never UPDATEs (ADR 0070 §3) — current-vs-superseded is derived
at read time. Drafts-only: writes solely to ``agent_outputs.facts``.

Required env:
  - BRAIN_PROJECT_ID
Optional env:
  - FACT_SOURCE_NOTE_KINDS     (default "email,inbox,calendar_event,capture")
  - FACT_MIN_CONFIDENCE        (default 0.7 — higher than commitments' 0.6)
  - FACT_MAX_NOTES_PER_TICK    (default 200)
  - BRAIN_VERTEX_LOCATION      (default us-central1)
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from .extractor import Extractor, ExtractorConfig
from .models import ENTITY_ACCOUNT, ENTITY_CONTACT, FactRow, SourceNote

log = logging.getLogger("agency_brain.agents.fact_extractor.main")

WATERMARK_CURSOR = "fact-extractor"

# (entity_id, entity_type) maps keyed by lowercased name / email.
ResolvedMaps = tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]


class NotesScanner(Protocol):
    def scan(self, *, since: str | None, kinds: list[str], limit: int) -> list[SourceNote]: ...


class EntityResolver(Protocol):
    def resolve(self, *, names: set[str], emails: set[str]) -> ResolvedMaps: ...


class FactsWriter(Protocol):
    def insert(self, rows: list[FactRow]) -> None: ...


class Watermark(Protocol):
    def read(self) -> str | None: ...
    def write(self, cursor: str) -> None: ...


def run_extraction(
    *,
    extractor: Extractor,
    scanner: NotesScanner,
    resolver: EntityResolver,
    writer: FactsWriter,
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
    names: set[str] = set()
    emails: set[str] = set()
    cost = 0.0
    max_seen = cursor
    for note in notes:
        try:
            result = extractor.extract(note=note, today=today)
        except Exception:
            log.exception("fact_extractor.extract_failed note_id=%s", note.note_id)
            continue
        cost += result.cost_usd
        for f in result.facts:
            if f.confidence < min_confidence:
                continue
            extracted.append((note, f))
            names.add(f.entity_name.lower())
            if f.entity_email:
                emails.add(f.entity_email.lower())
        if max_seen is None or note.ingested_at > max_seen:
            max_seen = note.ingested_at

    by_name, by_email = resolver.resolve(names=names, emails=emails) if extracted else ({}, {})

    rows: list[FactRow] = []
    for note, f in extracted:
        entity_id: str | None = None
        entity_type: str | None = None
        email_key = (f.entity_email or "").lower()
        name_key = f.entity_name.lower()
        if email_key and email_key in by_email:
            entity_id, entity_type = by_email[email_key]
        elif name_key in by_name:
            entity_id, entity_type = by_name[name_key]
        observed = f.observed_date or note.note_date or today
        rows.append(
            FactRow(
                fact_id=uuid.uuid4().hex,
                extracted_at=now_iso,
                observed_date=observed,
                entity_id=entity_id,
                entity_type=entity_type,
                entity_name=f.entity_name,
                predicate=f.predicate,
                value=f.value,
                source_note_id=note.note_id,
                source_note_kind=note.note_kind,
                confidence=f.confidence,
                agent_run_id=run_id,
            )
        )

    if rows:
        writer.insert(rows)
    if max_seen is not None and max_seen != cursor:
        watermark.write(max_seen)

    stats = {
        "scanned": len(notes),
        "facts_written": len(rows),
        "resolved": sum(1 for r in rows if r.entity_id),
        "cost_usd": round(cost, 6),
        "watermark_advanced": bool(max_seen and max_seen != cursor),
    }
    log.info("fact_extractor.done %s", stats)
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
            "FACT_SOURCE_NOTE_KINDS", "email,inbox,calendar_event,capture"
        ).split(",")
        if k.strip()
    ]
    min_confidence = float(os.environ.get("FACT_MIN_CONFIDENCE", "0.7"))
    max_notes = int(os.environ.get("FACT_MAX_NOTES_PER_TICK", "200"))

    from google.cloud import bigquery

    bq = bigquery.Client(project=project_id)
    run_id = uuid.uuid4().hex
    now = datetime.now(tz=UTC)

    log.info(
        "fact_extractor.start project=%s kinds=%s min_conf=%.2f run_id=%s",
        project_id,
        source_kinds,
        min_confidence,
        run_id,
    )
    try:
        run_extraction(
            extractor=Extractor(ExtractorConfig(project_id=project_id, location=location)),
            scanner=_BQNotesScanner(bq, project_id),
            resolver=_BQEntityResolver(bq, project_id),
            writer=_BQFactsWriter(bq, project_id),
            watermark=_BQWatermark(bq, project_id),
            source_kinds=source_kinds,
            min_confidence=min_confidence,
            max_notes=max_notes,
            run_id=run_id,
            today=now.date().isoformat(),
            now_iso=now.isoformat(),
        )
    except Exception:
        log.exception("fact_extractor.run_failed")
        return 1
    return 0


# ----------------------------------------------------- BQ adapters


class _BQNotesScanner:
    """Scan new, non-HIPAA, in-scope notes not already in facts."""

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
                f"`{self._p}.agent_outputs.facts` WHERE source_note_id IS NOT NULL)"
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
            f"SELECT note_id, note_kind, markdown_content, ingested_at, "  # noqa: S608
            f"DATE(COALESCE(created_at, ingested_at)) AS note_date "
            f"FROM `{self._p}.agent_outputs.notes` "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY ingested_at ASC LIMIT @limit"
        )
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        out: list[SourceNote] = []
        for r in self._bq.query(sql, job_config=job_config).result():
            ts = r.get("ingested_at")
            nd = r.get("note_date")
            out.append(
                SourceNote(
                    note_id=str(r.get("note_id") or ""),
                    note_kind=str(r.get("note_kind") or ""),
                    markdown_content=str(r.get("markdown_content") or ""),
                    ingested_at=ts.isoformat() if ts is not None else "",
                    note_date=nd.isoformat() if nd is not None else None,
                )
            )
        return out


class _BQEntityResolver:
    """Resolve entity name -> account, name/email -> contact (replica)."""

    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def resolve(self, *, names: set[str], emails: set[str]) -> ResolvedMaps:
        from google.cloud import bigquery

        by_name: dict[str, tuple[str, str]] = {}
        by_email: dict[str, tuple[str, str]] = {}
        if names:
            # Contacts by name first, then accounts by company_name OVERRIDE —
            # an account name match wins over a contact name match.
            try:
                csql = (
                    f"SELECT LOWER(name) AS k, _airtable_record_id AS id "  # noqa: S608
                    f"FROM `{self._p}.airtable_replica.contacts` "
                    f"WHERE LOWER(name) IN UNNEST(@names)"
                )
                for r in self._bq.query(
                    csql,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ArrayQueryParameter("names", "STRING", sorted(names))
                        ]
                    ),
                ).result():
                    if r.get("k") and r.get("id"):
                        by_name[str(r["k"])] = (str(r["id"]), ENTITY_CONTACT)
                asql = (
                    f"SELECT LOWER(company_name) AS k, _airtable_record_id AS id "  # noqa: S608
                    f"FROM `{self._p}.airtable_replica.accounts` "
                    f"WHERE LOWER(company_name) IN UNNEST(@names)"
                )
                for r in self._bq.query(
                    asql,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ArrayQueryParameter("names", "STRING", sorted(names))
                        ]
                    ),
                ).result():
                    if r.get("k") and r.get("id"):
                        by_name[str(r["k"])] = (str(r["id"]), ENTITY_ACCOUNT)
            except Exception:
                log.exception("fact_extractor.name_resolve_failed")
        if emails:
            try:
                esql = (
                    f"SELECT LOWER(email) AS k, _airtable_record_id AS id "  # noqa: S608
                    f"FROM `{self._p}.airtable_replica.contacts` "
                    f"WHERE LOWER(email) IN UNNEST(@emails)"
                )
                for r in self._bq.query(
                    esql,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ArrayQueryParameter("emails", "STRING", sorted(emails))
                        ]
                    ),
                ).result():
                    if r.get("k") and r.get("id"):
                        by_email[str(r["k"])] = (str(r["id"]), ENTITY_CONTACT)
            except Exception:
                log.exception("fact_extractor.email_resolve_failed")
        return by_name, by_email


class _BQFactsWriter:
    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def insert(self, rows: list[FactRow]) -> None:
        table = f"{self._p}.agent_outputs.facts"
        payload = [
            {
                "fact_id": r.fact_id,
                "extracted_at": r.extracted_at,
                "observed_date": r.observed_date,
                "entity_id": r.entity_id,
                "entity_type": r.entity_type,
                "entity_name": r.entity_name,
                "predicate": r.predicate,
                "value": r.value,
                "source_note_id": r.source_note_id,
                "source_note_kind": r.source_note_kind,
                "confidence": r.confidence,
                "agent_run_id": r.agent_run_id,
            }
            for r in rows
        ]
        errors = self._bq.insert_rows_json(table, payload)
        if errors:
            raise RuntimeError(f"facts insert failed: {errors}")


class _BQWatermark:
    """Insert-only cursor in agent_state.fact_extractor_watermark."""

    def __init__(self, bq: Any, project_id: str) -> None:
        self._bq = bq
        self._p = project_id

    def read(self) -> str | None:
        sql = (
            f"SELECT last_ingested_at_seen FROM "  # noqa: S608
            f"`{self._p}.agent_state.fact_extractor_watermark` "
            f"WHERE cursor_name = '{WATERMARK_CURSOR}' "
            f"ORDER BY updated_at DESC LIMIT 1"
        )
        try:
            for r in self._bq.query(sql).result():
                ts = r.get("last_ingested_at_seen")
                return ts.isoformat() if ts is not None else None
        except Exception:
            log.exception("fact_extractor.watermark_read_failed")
        return None

    def write(self, cursor: str) -> None:
        table = f"{self._p}.agent_state.fact_extractor_watermark"
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
