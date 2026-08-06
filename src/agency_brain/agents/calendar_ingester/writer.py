"""MERGE writer for ``agent_outputs.notes`` (calendar_event rows).

Idempotent re-runs: keyed on ``external_id = event.id``. If a row with
that external_id exists and its ``embedding_content_hash`` matches the
new content, the MERGE leaves it unchanged. Otherwise, the row is
inserted (first time) or updated (event was edited).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Protocol

from .models import IngestResult, NoteRow

log = logging.getLogger("agency_brain.agents.calendar_ingester.writer")

# Rows-per-MERGE cap. The whole batch travels as one @rows_json STRING
# parameter, and BQ caps a scalar query parameter at ~1 MB. Each row carries
# a 768-float embedding (~8 KB of JSON) plus the event markdown, so a dense
# multi-calendar window can cross 1 MB. 50 rows keeps each call well under
# the cap while staying cheap on DML slots.
MERGE_BATCH_SIZE = 50


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class CalendarWriter:
    """MERGE-on-external_id writer for calendar event rows."""

    def __init__(
        self,
        *,
        bq_query: BQQueryClient,
        project_id: str,
        notes_table: str = "notes",
        dataset_id: str = "agent_outputs",
    ) -> None:
        self._bq_query = bq_query
        self._notes_ref = f"{project_id}.{dataset_id}.{notes_table}"

    def merge(self, rows: Iterable[NoteRow]) -> IngestResult:
        rows = list(rows)
        if not rows:
            return IngestResult(
                total_events=0,
                inserted=0,
                updated=0,
                unchanged=0,
                skipped_hipaa=0,
                embed_failures=0,
                errors=0,
            )

        # Pull existing external_ids → content_hash map for the rows we
        # care about so we can classify each NoteRow as
        # insert/update/unchanged BEFORE issuing the MERGE.
        external_ids = [r.external_id for r in rows if r.external_id]
        existing = self._existing_hashes(external_ids)

        inserted = 0
        updated = 0
        unchanged = 0
        for row in rows:
            prior = existing.get(row.external_id)
            if prior is None:
                inserted += 1
            elif prior == row.embedding_content_hash:
                unchanged += 1
            else:
                updated += 1

        # Issue the MERGE (skip rows that are unchanged at the SQL layer
        # too, so the BQ slot cost is bounded — but for simplicity v1
        # MERGEs everything; BQ's MERGE is idempotent on identical rows).
        try:
            self._merge_rows(rows)
        except Exception:
            log.exception("calendar_ingester.writer.merge_failed")
            return IngestResult(
                total_events=len(rows),
                inserted=0,
                updated=0,
                unchanged=0,
                skipped_hipaa=0,
                embed_failures=0,
                errors=len(rows),
            )

        return IngestResult(
            total_events=len(rows),
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            skipped_hipaa=0,  # tracked by caller (filter runs before transform)
            embed_failures=0,  # tracked by transformer
            errors=0,
        )

    # ------------------------------------------------------------------ helpers

    def _existing_hashes(self, external_ids: list[str]) -> dict[str, str]:
        if not external_ids:
            return {}
        sql = (
            f"SELECT external_id, embedding_content_hash FROM `{self._notes_ref}` "  # noqa: S608
            "WHERE external_id IN UNNEST(@ids) AND note_kind = 'calendar_event'"
        )
        params = [{"name": "ids", "type": "ARRAY_STRING", "value": external_ids}]
        try:
            rows = self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("calendar_ingester.writer.existing_lookup_failed")
            return {}
        return {
            str(r.get("external_id") or ""): str(r.get("embedding_content_hash") or "")
            for r in rows
            if r.get("external_id")
        }

    def _merge_rows(self, rows: list[NoteRow]) -> None:
        """Run MERGE statement(s) against agent_outputs.notes.

        We use a parametrized JSON array → UNNEST(JSON_QUERY_ARRAY(...))
        pattern so the call shape is bounded and the row payload travels
        as a single STRING parameter. The BQ-side type contract for
        embedding ARRAY<FLOAT64> + event_metadata STRUCT is handled by
        explicit casts inside the source SELECT.

        Rows are chunked at ``MERGE_BATCH_SIZE`` so the @rows_json parameter
        stays under BQ's ~1 MB scalar-parameter limit. Each chunk is keyed
        on external_id, so per-chunk MERGE is still idempotent.
        """
        sql = self._merge_sql()
        for start in range(0, len(rows), MERGE_BATCH_SIZE):
            chunk = rows[start : start + MERGE_BATCH_SIZE]
            rows_json = json.dumps([_to_json_dict(r) for r in chunk])
            params = [
                {"name": "rows_json", "type": "STRING", "value": rows_json},
            ]
            self._bq_query.query_rows(sql, parameters=params)

    def _merge_sql(self) -> str:
        # _notes_ref is built from BRAIN_PROJECT_ID env (Cloud Run config),
        # not user input — no injection vector. Static dataset/table.
        return _MERGE_SQL_TEMPLATE.format(notes_ref=self._notes_ref)


_MERGE_SQL_TEMPLATE = """
MERGE `{notes_ref}` AS T
USING (
  SELECT
    JSON_VALUE(row_json, '$.note_id')                            AS note_id,
    JSON_VALUE(row_json, '$.filename')                           AS filename,
    JSON_VALUE(row_json, '$.markdown_content')                   AS markdown_content,
    JSON_VALUE(row_json, '$.source_drive_url')                   AS source_drive_url,
    JSON_VALUE(row_json, '$.scope')                              AS scope,
    JSON_VALUE(row_json, '$.note_kind')                          AS note_kind,
    CAST(JSON_VALUE(row_json, '$.hipaa_isolated') AS BOOL)       AS hipaa_isolated,
    JSON_VALUE(row_json, '$.external_id')                        AS external_id,
    JSON_VALUE(row_json, '$.revision_id')                        AS revision_id,
    JSON_VALUE(row_json, '$.source_drive_file_id')               AS source_drive_file_id,
    TIMESTAMP(JSON_VALUE(row_json, '$.created_at'))              AS created_at,
    CAST(JSON_VALUE(row_json, '$.extraction_confidence') AS FLOAT64) AS extraction_confidence,
    CAST(JSON_VALUE(row_json, '$.page_count') AS INT64)          AS page_count,
    ARRAY(
      SELECT CAST(v AS FLOAT64)
      FROM UNNEST(JSON_VALUE_ARRAY(row_json, '$.embedding')) AS v
    )                                                            AS embedding,
    JSON_VALUE(row_json, '$.embedding_model')                    AS embedding_model,
    JSON_VALUE(row_json, '$.embedding_content_hash')             AS embedding_content_hash,
    STRUCT(
      JSON_VALUE(row_json, '$.event_metadata.start')            AS start,
      JSON_VALUE(row_json, '$.event_metadata.end')              AS `end`,
      ARRAY(
        SELECT v FROM UNNEST(JSON_VALUE_ARRAY(row_json, '$.event_metadata.attendees')) AS v
      )                                                          AS attendees,
      JSON_VALUE(row_json, '$.event_metadata.organizer')        AS organizer,
      JSON_VALUE(row_json, '$.event_metadata.location')         AS location,
      JSON_VALUE(row_json, '$.event_metadata.status')           AS status
    )                                                            AS event_metadata,
    TIMESTAMP(JSON_VALUE(row_json, '$.ingested_at'))            AS ingested_at,
    JSON_VALUE(row_json, '$.extraction_method')                  AS extraction_method
  FROM UNNEST(JSON_QUERY_ARRAY(@rows_json)) AS row_json
) AS S
ON T.external_id = S.external_id AND T.note_kind = 'calendar_event'
WHEN MATCHED AND T.embedding_content_hash != S.embedding_content_hash THEN
  UPDATE SET
    filename = S.filename,
    markdown_content = S.markdown_content,
    source_drive_url = S.source_drive_url,
    scope = S.scope,
    revision_id = S.revision_id,
    embedding = S.embedding,
    embedding_model = S.embedding_model,
    embedding_content_hash = S.embedding_content_hash,
    event_metadata = S.event_metadata,
    ingested_at = S.ingested_at,
    extraction_method = S.extraction_method
WHEN NOT MATCHED THEN
  INSERT (note_id, filename, markdown_content, source_drive_url,
          scope, note_kind, hipaa_isolated, external_id, revision_id,
          source_drive_file_id, created_at, extraction_confidence, page_count,
          embedding, embedding_model, embedding_content_hash,
          event_metadata, ingested_at, extraction_method)
  VALUES (S.note_id, S.filename, S.markdown_content, S.source_drive_url,
          S.scope, S.note_kind, S.hipaa_isolated, S.external_id, S.revision_id,
          S.source_drive_file_id, S.created_at, S.extraction_confidence, S.page_count,
          S.embedding, S.embedding_model, S.embedding_content_hash,
          S.event_metadata, S.ingested_at, S.extraction_method)
"""


def _to_json_dict(row: NoteRow) -> dict:
    return {
        "note_id": row.note_id,
        "filename": row.filename,
        "markdown_content": row.markdown_content,
        "source_drive_url": row.source_drive_url,
        "scope": row.scope,
        "note_kind": row.note_kind,
        "hipaa_isolated": row.hipaa_isolated,
        "external_id": row.external_id,
        "revision_id": row.revision_id,
        "source_drive_file_id": row.source_drive_file_id,
        "created_at": row.created_at.isoformat(),
        "extraction_confidence": row.extraction_confidence,
        "page_count": row.page_count,
        "embedding": list(row.embedding),
        "embedding_model": row.embedding_model,
        "embedding_content_hash": row.embedding_content_hash,
        "event_metadata": row.event_metadata.to_bq_struct(),
        "ingested_at": row.ingested_at.isoformat(),
        "extraction_method": row.extraction_method,
    }
