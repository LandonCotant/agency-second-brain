"""Run-checkpoint persistence for the CRM Auto-updater (ADR 0047).

Stores one row per run in ``agent_outputs.crm_updater_runs``. The
since-cursor is the most recent successful run's ``ended_at`` (we
filter ``users.messages.list`` by `after:` UNIX timestamp downstream).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from .models import RunCheckpoint

log = logging.getLogger("agency_brain.agents.crm_updater.dedup")


class BQRowsClient(Protocol):
    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class RunCheckpointStore:
    """Read + write `agent_outputs.crm_updater_runs`."""

    def __init__(
        self,
        *,
        bq_query: BQQueryClient,
        bq_writer: BQRowsClient,
        project_id: str,
        table: str = "crm_updater_runs",
        dataset_id: str = "agent_outputs",
    ) -> None:
        self._bq_query = bq_query
        self._bq_writer = bq_writer
        self._table_ref = f"{project_id}.{dataset_id}.{table}"

    def latest_successful_run(self) -> RunCheckpoint | None:
        """Most recent ``success = TRUE`` run; None if no runs yet."""
        sql = f"SELECT run_id, started_at, ended_at, history_id_after, messages_processed, drafts_created, errors, success FROM `{self._table_ref}` WHERE success = TRUE ORDER BY ended_at DESC LIMIT 1"  # noqa: S608, E501
        try:
            rows = self._bq_query.query_rows(sql)
        except Exception:
            log.exception("crm_updater.dedup.latest_lookup_failed")
            return None
        if not rows:
            return None
        return _row_to_checkpoint(rows[0])

    def write(self, checkpoint: RunCheckpoint) -> None:
        try:
            errors = self._bq_writer.insert_rows_json(
                self._table_ref, [_checkpoint_to_row(checkpoint)]
            )
        except Exception:
            log.exception("crm_updater.dedup.write_failed run_id=%s", checkpoint.run_id)
            return
        if errors:
            log.warning("crm_updater.dedup.write_partial errors=%s", errors)


def new_run_id() -> str:
    return f"crmu-{uuid.uuid4().hex[:16]}"


def _row_to_checkpoint(row: dict) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=str(row.get("run_id") or ""),
        started_at=_parse_ts(row.get("started_at")),
        ended_at=_parse_ts(row.get("ended_at")),
        history_id_after=row.get("history_id_after"),
        messages_processed=int(row.get("messages_processed") or 0),
        drafts_created=int(row.get("drafts_created") or 0),
        errors=int(row.get("errors") or 0),
        success=bool(row.get("success")),
    )


def _checkpoint_to_row(c: RunCheckpoint) -> dict:
    return {
        "run_id": c.run_id,
        "started_at": _ts(c.started_at),
        "ended_at": _ts(c.ended_at),
        "history_id_after": c.history_id_after,
        "messages_processed": c.messages_processed,
        "drafts_created": c.drafts_created,
        "errors": c.errors,
        "success": c.success,
    }


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_ts(raw: object) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
