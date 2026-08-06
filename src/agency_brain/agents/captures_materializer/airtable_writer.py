"""Mutate Airtable ``Captures`` rows after a successful materialize (ADR 0039 §3).

Two operations:

  ``flip_synced(record_id)``  → set ``Synced = TRUE``, ``Synced At = NOW()``
  ``delete_capture(record_id)`` → DELETE the Airtable row

Per ADR 0039 §3, both operations run synchronously after the BQ write
in the orchestrator's per-row loop. Failure modes are tolerated by the
deterministic dedup key (``captures-{record_id}``) on the BQ side.

Auth: ``airtable-tasks-write-pat-prod`` Secret. Same PAT the Triage
Agent's ``TaskDrafter`` uses — ADR 0039 §Consequences notes the scope
widening is acceptable because the underlying PAT was issued with
``data.records:write`` on the Operations base.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger("agency_brain.agents.captures_materializer.airtable_writer")


class AirtableTableClient(Protocol):
    """Minimal pyairtable.Table surface — only what we use."""

    def update(self, record_id: str, fields: dict) -> dict: ...

    def delete(self, record_id: str) -> dict: ...


class AirtableWriteError(RuntimeError):
    pass


class CapturesAirtableWriter:
    """Encapsulates the two mutation calls needed per materialized row."""

    def __init__(self, *, table: AirtableTableClient) -> None:
        self._table = table

    def flip_synced(self, record_id: str, *, now: datetime | None = None) -> None:
        """Set Synced=TRUE and Synced At=now on the Airtable row."""
        ts = (now or datetime.now(UTC)).date().isoformat()
        try:
            self._table.update(
                record_id,
                {"Synced": True, "Synced At": ts},
            )
        except Exception as exc:
            raise AirtableWriteError(
                f"Captures.update failed for {record_id}: " f"{type(exc).__name__}: {exc}"
            ) from exc

    def delete_capture(self, record_id: str) -> None:
        try:
            self._table.delete(record_id)
        except Exception as exc:
            raise AirtableWriteError(
                f"Captures.delete failed for {record_id}: " f"{type(exc).__name__}: {exc}"
            ) from exc


def build_table_client(
    *,
    base_id: str,
    pat: str,
    table_name: str = "Captures",
) -> Any:
    """Factory for the production pyairtable Table.

    Lazy-imports pyairtable so unit tests don't pull the dep.
    """
    from pyairtable import Api

    api = Api(pat)
    return api.table(base_id, table_name)
