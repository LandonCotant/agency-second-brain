"""Audit log client for `agent_audit_log.events`.

Every agent invocation writes one row via `AuditLogClient.emit`. Streaming
inserts are used so PRD §1.5 ("queryable within seconds") holds.

The base agent class is the only sanctioned caller; WS-G subclasses get the
guarantee of a row on every code path through `BaseAgent.invoke`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import AuditEvent

if TYPE_CHECKING:
    from google.cloud import bigquery


class AuditLogWriteError(RuntimeError):
    """Raised when BigQuery rejects the streaming insert.

    PRD §8.2 alerts on "Audit log write failure ≥ 1 event" — that monitor
    is owned by WS-E and triggers off the BigQuery error metric. Raising here
    surfaces the failure to the caller; the base agent re-raises so the
    invocation aborts loudly rather than silently dropping the audit row.
    """


class AuditLogClient:
    def __init__(
        self,
        project_id: str,
        dataset_id: str = "agent_audit_log",
        table_id: str = "events",
        bq_client: Any = None,
    ) -> None:
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._table_id = table_id
        self._bq_client = bq_client

    @property
    def table_ref(self) -> str:
        return f"{self._project_id}.{self._dataset_id}.{self._table_id}"

    def _client(self) -> bigquery.Client:
        if self._bq_client is None:
            from google.cloud import bigquery

            self._bq_client = bigquery.Client(project=self._project_id)
        return self._bq_client

    def emit(self, event: AuditEvent) -> None:
        client = self._client()
        errors = client.insert_rows_json(self.table_ref, [event.to_bq_row()])
        if errors:
            raise AuditLogWriteError(
                f"audit log write failed for event_id={event.event_id}: {errors}"
            )
