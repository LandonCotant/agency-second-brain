"""``RiskFlagsWriter`` — BQ insert + same-day dedup pre-check (ADR 0033).

Mirrors ``agents/triage/writer.py``'s shape: a small wrapper around a
BQ client that:

  1. SELECT-pre-checks for an existing row at
     ``(account_id, pattern_name, DATE(flagged_at))``;
  2. when no hit, INSERTs the flag row and returns ``written=True``;
  3. when hit, returns ``written=False`` and the existing
     ``flag_id`` so the audit log surfaces it.

Per ADR 0026 streaming-buffer caveat, dedup is INSERT-only — no DML
on the same row twice in a 30-min window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WriteResult:
    written: bool
    """True when the row was inserted; False on dedup-skip."""

    flag_id: str
    """Newly-inserted flag_id when ``written=True``; the existing
    flag_id when ``written=False`` (dedup hit)."""

    suppressed: bool = False
    """True when an operator ``noise`` verdict (ADR 0060) matched this
    flag's ``(account_id, pattern_name)`` tuple, so the row was written
    *pre-resolved* (``resolved_at`` set) — invisible to ``open_risk_flags``
    and the WS-D fan-out poll, which both filter ``resolved_at IS NULL``."""

    suppressed_by: str | None = None
    """The ``signal_feedback.feedback_id`` that drove the suppression,
    for the structured log line + audit trail. ``None`` unless suppressed."""


class BQRowsClient(Protocol):
    """Subset of ``google.cloud.bigquery.Client`` we depend on."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Subset used for the dedup pre-check."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class RiskFlagsWriter:
    """Inserts one row into ``agent_outputs.risk_flags`` per fired flag.

    Dedup window: same calendar day in UTC, keyed
    ``(account_id, pattern_name)``. Same posture as ADR 0026's
    triage dedup: a recurring signal on the same day is suppressed
    at insert time rather than mutating an existing row.
    """

    def __init__(
        self,
        *,
        rows_client: BQRowsClient,
        query_client: BQQueryClient | None,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "risk_flags",
        feedback_table_id: str = "signal_feedback",
    ) -> None:
        self._rows = rows_client
        self._query = query_client
        self._project_id = project_id
        self._dataset_id = dataset_id
        self._table_id = table_id
        self._feedback_table_id = feedback_table_id

    @property
    def table_ref(self) -> str:
        return f"{self._project_id}.{self._dataset_id}.{self._table_id}"

    @property
    def feedback_table_ref(self) -> str:
        return f"{self._project_id}.{self._dataset_id}.{self._feedback_table_id}"

    # ----------------------------------------------------------- write

    def write(self, row: dict[str, Any]) -> WriteResult:
        """Suppression-gate, then dedup-pre-check, then INSERT.

        ``row`` is the dict produced by
        ``RiskWatcher.materialize_flag_row``. Required keys:
        ``flag_id``, ``account_id``, ``pattern_name``, ``flagged_at``.

        ADR 0060 §3 — if an operator ``noise`` verdict is active for this
        flag's ``(account_id, pattern_name)`` tuple, the row is written
        *pre-resolved* (``resolved_at = flagged_at``, ``resolution_note``
        references the feedback id) rather than dropped. Both consumers
        (``open_risk_flags``, the WS-D fan-out poll) filter
        ``resolved_at IS NULL``, so a pre-resolved row is invisible to the
        brief and the fan-out while leaving a queryable audit trail in
        ``risk_flags`` itself. The same-day dedup is skipped on the
        suppressed path: it only matches *unresolved* rows, so it could
        never collapse a pre-resolved one anyway.
        """
        suppression = self._find_active_suppression(
            account_id=row["account_id"],
            pattern_name=row["pattern_name"],
        )
        if suppression is not None:
            feedback_id = suppression
            suppressed_row = {
                **row,
                "resolved_at": row["flagged_at"],
                "resolution_note": f"auto-suppressed: feedback {feedback_id}",
            }
            errors = self._rows.insert_rows_json(self.table_ref, [suppressed_row])
            if errors:
                raise RiskFlagsWriteError(f"BQ insert into {self.table_ref} failed: {errors!r}")
            return WriteResult(
                written=True,
                flag_id=row["flag_id"],
                suppressed=True,
                suppressed_by=feedback_id,
            )

        existing = self._find_existing_same_day(
            account_id=row["account_id"],
            pattern_name=row["pattern_name"],
            flagged_at=row["flagged_at"],
        )
        if existing is not None:
            return WriteResult(written=False, flag_id=existing)

        errors = self._rows.insert_rows_json(self.table_ref, [row])
        if errors:
            raise RiskFlagsWriteError(f"BQ insert into {self.table_ref} failed: {errors!r}")
        return WriteResult(written=True, flag_id=row["flag_id"])

    # --------------------------------------------- suppression pre-check

    def _find_active_suppression(self, *, account_id: str, pattern_name: str) -> str | None:
        """Return the ``feedback_id`` of an active ``noise`` verdict for
        this ``(account_id, pattern_name)`` tuple, or ``None``.

        Active = ``scope='risk'`` AND ``verdict='noise'`` AND the verdict
        targets this pattern (or all patterns on the account, i.e.
        ``pattern_name IS NULL``) AND it has not expired
        (``mute_until IS NULL OR mute_until > CURRENT_TIMESTAMP()``).
        Most recent verdict wins.

        Returns ``None`` when no query client is wired (test/dev) — same
        posture as the dedup pre-check; production always passes one.
        """
        if self._query is None:
            return None

        sql = (
            f"SELECT feedback_id FROM `{self.feedback_table_ref}` "  # noqa: S608
            "WHERE scope = 'risk' "
            "AND verdict = 'noise' "
            "AND account_id = @account_id "
            "AND (pattern_name = @pattern_name OR pattern_name IS NULL) "
            "AND (mute_until IS NULL OR mute_until > CURRENT_TIMESTAMP()) "
            "ORDER BY created_at DESC LIMIT 1"
        )
        params = [
            {"name": "account_id", "type": "STRING", "value": account_id},
            {"name": "pattern_name", "type": "STRING", "value": pattern_name},
        ]
        rows = self._query.query_rows(sql, params)
        if not rows:
            return None
        return rows[0]["feedback_id"]

    # --------------------------------------------------- dedup pre-check

    def _find_existing_same_day(
        self, *, account_id: str, pattern_name: str, flagged_at: str
    ) -> str | None:
        """Return existing flag_id when a same-day match is found.

        Returns ``None`` when no query client is wired (test/dev) — in
        that case dedup is skipped and every call inserts. Production
        callers always pass a real query client.
        """
        if self._query is None:
            return None

        # `flagged_at` is the to-be-inserted timestamp; we extract its
        # calendar date for the WHERE clause. UTC matches the BQ
        # partitioning column convention.
        # `resolved_at IS NULL` excludes flags that an operator has manually
        # retired so the next signal write on the same calendar day proceeds
        # (mirrors the routing-side filter from ADR 0035 §5).
        sql = (
            f"SELECT flag_id FROM `{self.table_ref}` "  # noqa: S608
            "WHERE account_id = @account_id "
            "AND pattern_name = @pattern_name "
            "AND DATE(flagged_at) = DATE(@flagged_at) "
            "AND resolved_at IS NULL "
            "ORDER BY flagged_at DESC LIMIT 1"
        )
        params = [
            {"name": "account_id", "type": "STRING", "value": account_id},
            {"name": "pattern_name", "type": "STRING", "value": pattern_name},
            {"name": "flagged_at", "type": "TIMESTAMP", "value": flagged_at},
        ]
        rows = self._query.query_rows(sql, params)
        if not rows:
            return None
        return rows[0]["flag_id"]


class RiskFlagsWriteError(RuntimeError):
    """Raised when a BQ insert into risk_flags returns row errors."""
