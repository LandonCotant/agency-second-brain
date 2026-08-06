"""Read unsynced captures from ``airtable_replica.captures`` (ADR 0039).

The replica is refreshed every 15 min by the WRITE_TRUNCATE airtable
sync (ADR 0010), so the materializer's view of the Airtable state is
at most one sync cycle stale. Filtering on ``synced = FALSE`` keeps
already-materialized rows out of subsequent ticks until the next sync
cycle re-replicates the post-flip state.

Schema is auto-generated from ``airtable/schema.json`` by
``terraform/modules/data_pipeline/replica_tables.tf`` — Airtable column
names slugify to ``captured_at`` / ``note_text`` / ``kind`` /
``scope_hint`` / ``synced`` / ``synced_at``. System columns ride along
as ``_airtable_record_id`` etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Capture, CaptureKind, CaptureScope


class BQQueryClient(Protocol):
    """Minimal SELECT surface — main.py adapts ``google.cloud.bigquery``."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class CapturesReader:
    """Loads unsynced ``airtable_replica.captures`` rows for materialization."""

    def __init__(
        self,
        *,
        bq_query: BQQueryClient,
        project_id: str,
        dataset_id: str = "airtable_replica",
        table_id: str = "captures",
    ) -> None:
        self._query = bq_query
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"

    @property
    def table_ref(self) -> str:
        return self._table_ref

    def read_unsynced(self, *, limit: int = 100) -> list[Capture]:
        """Return up to ``limit`` rows where Synced is FALSE/NULL.

        Rows missing ``kind`` or ``note_text`` are silently filtered —
        the form makes both required, but a partial-write race during a
        WRITE_TRUNCATE sync could surface a half-row. The next tick will
        pick it up cleanly.
        """
        sql = (
            "SELECT "
            "  _airtable_record_id AS record_id, "
            "  captured_at, "
            "  note_text, "
            "  kind, "
            "  scope_hint "
            f"FROM `{self._table_ref}` "  # — table_ref is internal
            "WHERE COALESCE(synced, FALSE) = FALSE "
            "ORDER BY captured_at ASC "
            "LIMIT @limit"
        )
        rows = self._query.query_rows(
            sql,
            parameters=[{"name": "limit", "type": "INT64", "value": limit}],
        )
        out: list[Capture] = []
        for r in rows:
            cap = _to_capture(r)
            if cap is not None:
                out.append(cap)
        return out


def _to_capture(row: dict) -> Capture | None:
    record_id = row.get("record_id")
    note_text = row.get("note_text")
    kind_raw = row.get("kind")
    if not record_id or not note_text or not kind_raw:
        return None
    try:
        kind = CaptureKind(kind_raw)
    except ValueError:
        return None
    scope_raw = row.get("scope_hint") or "personal"
    try:
        scope = CaptureScope(scope_raw)
    except ValueError:
        scope = CaptureScope.PERSONAL
    captured_at = row.get("captured_at")
    if isinstance(captured_at, str):
        captured_at = datetime.fromisoformat(captured_at)
    if not isinstance(captured_at, datetime):
        return None
    return Capture(
        record_id=record_id,
        captured_at=captured_at,
        note_text=note_text,
        kind=kind,
        scope=scope,
    )
