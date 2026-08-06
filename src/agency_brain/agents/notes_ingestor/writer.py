"""BQ writers for the notes ingestor (ADR 0031).

Two write surfaces:

  NotesWriter   → ``agent_outputs.notes``      (keyed on note_id)
  WatermarkStore→ ``agent_state.notes_ingestor_watermark`` (one row per folder)

``NotesWriter`` exposes two write paths:

  - ``write`` — ``insert_rows_json`` (no DML; ADR 0025 streaming-buffer
    posture). The Notes Ingestor inbox flow uses this with a pre-INSERT
    SELECT keyed ``(source_drive_file_id, revision_id)`` so re-listing a
    file before the move-to-processed step doesn't double-write.
  - ``merge_by_note_id`` — a MERGE keyed on ``note_id`` (== Drive
    ``file_id``). The Librarian (galaxy / area / resource) uses this so a
    *changed* file UPDATEs its single row instead of appending a new
    revision row every sweep (ADR 0071 — the galaxy/people accumulation
    fix). Safe past the streaming buffer because Librarian sweeps are
    days apart, so the prior row is always promoted out of the buffer.

Watermark advances are MERGE-by-folder via parameterized SELECT + UPSERT
— we accept that watermark state is small enough that a per-folder full
row replace is fine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .models import NoteRow


class BQRowsClient(Protocol):
    """Matches ``google.cloud.bigquery.Client.insert_rows_json``."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Parameterized SELECT for the dedup pre-check + watermark read."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class BQUpdateClient(Protocol):
    """Parameterized UPDATE for the embeddings backfill (ADR 0039 §4).

    Returned int is the number of rows affected. Implementations adapt
    ``google.cloud.bigquery.Client.query`` with a job config.
    """

    def update_rows(self, sql: str, parameters: list[dict] | None = None) -> int: ...


class NotesWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class DedupHit:
    note_id: str
    ingested_at: datetime


class NotesWriter:
    """Insert one row into ``agent_outputs.notes`` with revision-keyed dedup."""

    def __init__(
        self,
        *,
        bq_rows: BQRowsClient,
        bq_query: BQQueryClient | None,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "notes",
        bq_update: BQUpdateClient | None = None,
    ) -> None:
        self._rows = bq_rows
        self._query = bq_query
        self._update = bq_update
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"

    @property
    def table_ref(self) -> str:
        return self._table_ref

    def find_existing(self, *, source_drive_file_id: str, revision_id: str) -> DedupHit | None:
        """Return existing row's note_id when (file_id, revision_id) hits.

        Returns None when no query client is wired (test-default) or no
        row is found.
        """
        if self._query is None:
            return None
        sql = (
            f"SELECT note_id, ingested_at FROM `{self._table_ref}` "  # noqa: S608
            "WHERE source_drive_file_id = @file_id "
            "AND revision_id = @revision_id "
            "ORDER BY ingested_at DESC LIMIT 1"
        )
        rows = self._query.query_rows(
            sql,
            parameters=[
                {"name": "file_id", "type": "STRING", "value": source_drive_file_id},
                {"name": "revision_id", "type": "STRING", "value": revision_id},
            ],
        )
        if not rows:
            return None
        first = rows[0]
        ts = first["ingested_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return DedupHit(note_id=first["note_id"], ingested_at=ts)

    def write(self, row: NoteRow) -> None:
        errors = self._rows.insert_rows_json(self._table_ref, [row.to_bq_row()])
        if errors:
            raise NotesWriteError(f"BQ rejected agent_outputs.notes insert: {errors}")

    def find_revision_by_note_id(self, *, note_id: str) -> str | None:
        """Return the most-recent row's ``revision_id`` for ``note_id``.

        ADR 0071 — the Librarian's write path keys idempotency on
        ``note_id`` (== Drive ``file_id``, stable across edits) so a
        re-ingest of a *changed* file UPDATEs the existing row instead of
        appending a new one. This is the "has anything changed?" pre-check:
        a matching ``revision_id`` means the content is unchanged and the
        caller can skip the (expensive) embed + MERGE entirely.

        Returns None when no query client is wired (test-default) or the
        note has never been ingested.
        """
        if self._query is None:
            return None
        sql = (
            f"SELECT revision_id FROM `{self._table_ref}` "  # noqa: S608
            "WHERE note_id = @note_id "
            "ORDER BY ingested_at DESC LIMIT 1"
        )
        rows = self._query.query_rows(
            sql,
            parameters=[{"name": "note_id", "type": "STRING", "value": note_id}],
        )
        if not rows:
            return None
        rev = rows[0].get("revision_id")
        return str(rev) if rev is not None else None

    def merge_by_note_id(self, row: NoteRow) -> None:
        """Upsert one row into ``agent_outputs.notes`` keyed on ``note_id``.

        ADR 0071 — replaces the INSERT-append write for Librarian-ingested
        kinds (galaxy / area / resource). ``note_id == source_drive_file_id``
        is globally unique (a file is one note), so a MERGE on ``note_id``
        alone keeps exactly one current row per note: a changed file UPDATEs
        in place rather than stacking a new revision row every sweep.

        Runs as a DML query job (``query_rows``), mirroring the calendar
        ingester's MERGE (ADR 0025 — DML can't touch streaming-buffer rows,
        but Librarian sweeps are days apart so the prior row is always past
        the buffer). ``triaged_item_id`` is intentionally left untouched on
        UPDATE so a Triage back-ref written after ingest is never clobbered.
        """
        if self._query is None:
            raise NotesWriteError("merge_by_note_id requires a BQQueryClient")
        rows_json = json.dumps([_merge_json_dict(row)])
        sql = _NOTE_ID_MERGE_TEMPLATE.format(table_ref=self._table_ref)
        self._query.query_rows(
            sql,
            parameters=[{"name": "rows_json", "type": "STRING", "value": rows_json}],
        )

    def find_unembedded_rows(self, *, limit: int) -> list[dict]:
        """Return rows in ``agent_outputs.notes`` that need an embedding.

        ADR 0039 §4 — backfill target is rows where ``embedding IS NULL``
        OR ``ARRAY_LENGTH(embedding) = 0``. Returns ``note_id`` +
        ``revision_id`` + ``markdown_content`` so the caller can embed
        and UPDATE without a second SELECT.

        Bounded by ``limit`` (sourced from ``MAX_BACKFILL_PER_TICK``).
        Empty rows (``markdown_content IS NULL OR = ''``) are
        intentionally included; the embedder short-circuits empty input
        to an empty vector and the UPDATE writes the content_hash so
        the next tick's SELECT will skip the row (because the row's
        embedding_model becomes non-NULL).
        """
        if self._query is None:
            return []
        sql = (
            "SELECT note_id, revision_id, markdown_content "
            f"FROM `{self._table_ref}` "
            "WHERE embedding IS NULL OR ARRAY_LENGTH(embedding) = 0 "
            "ORDER BY ingested_at ASC "
            "LIMIT @limit"
        )
        return self._query.query_rows(
            sql,
            parameters=[{"name": "limit", "type": "INT64", "value": limit}],
        )

    def update_embedding(
        self,
        *,
        note_id: str,
        revision_id: str,
        embedding: list[float],
        model: str,
        content_hash: str,
        generated_at: datetime,
    ) -> int:
        """Backfill / re-embed an existing row's embedding columns.

        Issues a parameterized UPDATE on ``(note_id, revision_id)``. ADR
        0039 §4 — pre-ADR-0038 rows have ``embedding IS NULL`` and need
        to be backfilled. ADR 0025 — UPDATE is allowed past the ~30-min
        streaming-buffer window; backfill targets are days/weeks old.

        Returns the number of rows updated (0 when no row matches the
        key — should never happen in practice because the caller passes
        keys read from the same table on the same tick).
        """
        if self._update is None:  # type: ignore[has-type]
            raise NotesWriteError("update_embedding requires a BQUpdateClient")
        sql = (
            f"UPDATE `{self._table_ref}` "  # noqa: S608 — table_ref is internal
            "SET embedding = @embedding, "
            "    embedding_model = @model, "
            "    embedding_content_hash = @content_hash, "
            "    embedding_generated_at = @generated_at "
            "WHERE note_id = @note_id AND revision_id = @revision_id"
        )
        return self._update.update_rows(
            sql,
            parameters=[
                {"name": "embedding", "type": "FLOAT64", "value": embedding, "mode": "REPEATED"},
                {"name": "model", "type": "STRING", "value": model},
                {"name": "content_hash", "type": "STRING", "value": content_hash},
                {
                    "name": "generated_at",
                    "type": "TIMESTAMP",
                    "value": generated_at.isoformat(),
                },
                {"name": "note_id", "type": "STRING", "value": note_id},
                {"name": "revision_id", "type": "STRING", "value": revision_id},
            ],
        )


class WatermarkStore:
    """Per-folder ``modifiedTime`` watermark in ``agent_state.notes_ingestor_watermark``.

    The table is keyed by ``folder_path``; we store the ISO ``modifiedTime``
    of the most-recently-ingested file plus the wall-clock ``updated_at``.
    Reads return ``None`` when the folder has never been seen — first
    list scans the entire folder.
    """

    def __init__(
        self,
        *,
        bq_rows: BQRowsClient,
        bq_query: BQQueryClient,
        project_id: str,
        dataset_id: str = "agent_state",
        table_id: str = "notes_ingestor_watermark",
    ) -> None:
        self._rows = bq_rows
        self._query = bq_query
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"

    @property
    def table_ref(self) -> str:
        return self._table_ref

    def read(self, folder_path: str) -> datetime | None:
        sql = (
            f"SELECT last_modified_time_seen FROM `{self._table_ref}` "  # noqa: S608
            "WHERE folder_path = @folder_path "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        rows = self._query.query_rows(
            sql,
            parameters=[
                {"name": "folder_path", "type": "STRING", "value": folder_path},
            ],
        )
        if not rows:
            return None
        ts = rows[0]["last_modified_time_seen"]
        if isinstance(ts, str):
            return datetime.fromisoformat(ts)
        return ts

    def write(self, *, folder_path: str, last_modified_time_seen: datetime) -> None:
        # Insert-only — readers take the most-recent row. Avoids DML on
        # streaming-buffer rows (ADR 0025). Per-folder row count is
        # one-per-tick at most; a daily compaction job can prune later
        # if it ever matters.
        row = {
            "folder_path": folder_path,
            "last_modified_time_seen": last_modified_time_seen.isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        errors = self._rows.insert_rows_json(self._table_ref, [row])
        if errors:
            raise NotesWriteError(f"BQ rejected watermark insert: {errors}")


# --------------------------------------------------------------- MERGE (ADR 0071)


def _merge_json_dict(row: NoteRow) -> dict:
    """Flatten a ``NoteRow`` to the JSON shape the MERGE source SELECT reads.

    Unlike ``NoteRow.to_bq_row`` (which omits unset PKM fields), every key
    is always present — JSON_VALUE paths must resolve, and an explicit
    ``null`` is what we want for an absent timestamp/embedding so the
    ``UPDATE``/``INSERT`` writes NULL rather than failing the path lookup.
    """
    return {
        "note_id": row.note_id,
        "revision_id": row.revision_id,
        "ingested_at": row.ingested_at.isoformat(),
        "created_at": row.created_at.isoformat(),
        "source_drive_file_id": row.source_drive_file_id,
        "source_drive_url": row.source_drive_url,
        "filename": row.filename,
        "markdown_content": row.markdown_content,
        "extraction_method": row.extraction_method.value,
        "extraction_confidence": row.extraction_confidence,
        "extraction_notes": row.extraction_notes,
        "page_count": row.page_count,
        "hipaa_isolated": row.hipaa_isolated,
        "note_kind": row.note_kind.value if row.note_kind is not None else None,
        "scope": row.scope.value if row.scope is not None else None,
        "embedding_content_hash": row.embedding_content_hash,
        "embedding": list(row.embedding),
        "embedding_model": row.embedding_model,
        "embedding_generated_at": (
            row.embedding_generated_at.isoformat()
            if row.embedding_generated_at is not None
            else None
        ),
    }


# Keyed on note_id alone: a Drive file is exactly one note, so this keeps a
# single current row per note. triaged_item_id is deliberately NOT in the
# UPDATE SET — a Triage back-ref written post-ingest must survive a re-sweep.
# table_ref is built from BRAIN_PROJECT_ID (Cloud Run config), not user input.
_NOTE_ID_MERGE_TEMPLATE = """
MERGE `{table_ref}` AS T
USING (
  SELECT
    JSON_VALUE(row_json, '$.note_id')                                AS note_id,
    JSON_VALUE(row_json, '$.revision_id')                            AS revision_id,
    TIMESTAMP(JSON_VALUE(row_json, '$.ingested_at'))                 AS ingested_at,
    TIMESTAMP(JSON_VALUE(row_json, '$.created_at'))                  AS created_at,
    JSON_VALUE(row_json, '$.source_drive_file_id')                  AS source_drive_file_id,
    JSON_VALUE(row_json, '$.source_drive_url')                      AS source_drive_url,
    JSON_VALUE(row_json, '$.filename')                              AS filename,
    JSON_VALUE(row_json, '$.markdown_content')                     AS markdown_content,
    JSON_VALUE(row_json, '$.extraction_method')                    AS extraction_method,
    CAST(JSON_VALUE(row_json, '$.extraction_confidence') AS FLOAT64) AS extraction_confidence,
    JSON_VALUE(row_json, '$.extraction_notes')                     AS extraction_notes,
    CAST(JSON_VALUE(row_json, '$.page_count') AS INT64)             AS page_count,
    CAST(JSON_VALUE(row_json, '$.hipaa_isolated') AS BOOL)         AS hipaa_isolated,
    JSON_VALUE(row_json, '$.note_kind')                            AS note_kind,
    JSON_VALUE(row_json, '$.scope')                                AS scope,
    JSON_VALUE(row_json, '$.embedding_content_hash')              AS embedding_content_hash,
    ARRAY(
      SELECT CAST(v AS FLOAT64)
      FROM UNNEST(JSON_VALUE_ARRAY(row_json, '$.embedding')) AS v
    )                                                              AS embedding,
    JSON_VALUE(row_json, '$.embedding_model')                     AS embedding_model,
    TIMESTAMP(JSON_VALUE(row_json, '$.embedding_generated_at'))    AS embedding_generated_at
  FROM UNNEST(JSON_QUERY_ARRAY(@rows_json)) AS row_json
) AS S
ON T.note_id = S.note_id
WHEN MATCHED THEN
  UPDATE SET
    revision_id = S.revision_id,
    ingested_at = S.ingested_at,
    created_at = S.created_at,
    source_drive_file_id = S.source_drive_file_id,
    source_drive_url = S.source_drive_url,
    filename = S.filename,
    markdown_content = S.markdown_content,
    extraction_method = S.extraction_method,
    extraction_confidence = S.extraction_confidence,
    extraction_notes = S.extraction_notes,
    page_count = S.page_count,
    hipaa_isolated = S.hipaa_isolated,
    note_kind = S.note_kind,
    scope = S.scope,
    embedding_content_hash = S.embedding_content_hash,
    embedding = S.embedding,
    embedding_model = S.embedding_model,
    embedding_generated_at = S.embedding_generated_at
WHEN NOT MATCHED THEN
  INSERT (note_id, revision_id, ingested_at, created_at, source_drive_file_id,
          source_drive_url, filename, markdown_content, extraction_method,
          extraction_confidence, extraction_notes, page_count, hipaa_isolated,
          note_kind, scope, embedding_content_hash, embedding, embedding_model,
          embedding_generated_at)
  VALUES (S.note_id, S.revision_id, S.ingested_at, S.created_at, S.source_drive_file_id,
          S.source_drive_url, S.filename, S.markdown_content, S.extraction_method,
          S.extraction_confidence, S.extraction_notes, S.page_count, S.hipaa_isolated,
          S.note_kind, S.scope, S.embedding_content_hash, S.embedding, S.embedding_model,
          S.embedding_generated_at)
"""
