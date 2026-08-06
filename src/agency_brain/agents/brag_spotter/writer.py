"""Brag Spotter wins writer + per-(week_of, title) dedup (ADR 0043 §5).

Lifts the `_row_exists` SELECT pattern from
`evening_reflection.extracted_writers`. Idempotency key:
``win_id = f"brag_spotter-{week_of_iso}-{title_hash12(title)}"``.

Title normalization (`title_hash12`) is shared with Reflection v2's
extracted-wins writer — re-imported here so a future tweak to the
algorithm flows through both writers. Trivial LLM phrasing variation
collapses to the same key, so the second Sunday's run on the same
week is a no-op (`written=False` outcomes only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

# Reuse the same normalize/hash helper Reflection v2 uses so wins
# extracted by either path share the canonical title key.
from ..evening_reflection.extracted_writers import title_hash12
from .models import WinCandidate

log = logging.getLogger("agency_brain.agents.brag_spotter.writer")


class BQRowsClient(Protocol):
    """`insert_rows_json` surface."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Parameterized SELECT for the pre-INSERT dedup."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class WriteOutcome:
    win_id: str
    written: bool
    error: str | None = None


def win_id_for(*, week_of: date, title: str) -> str:
    return f"brag_spotter-{week_of.isoformat()}-{title_hash12(title)}"


def build_win_row(
    candidate: WinCandidate,
    *,
    win_id: str,
    week_of: date,
    agent_run_id: str | None,
    now: datetime,
) -> dict:
    """Map a `WinCandidate` onto an `agent_outputs.wins` row.

    `source_kind` carries the per-source enum (`triaged_item | routed_event
    | note | decision | reflection`) so consumers can JOIN back to the
    originating table cleanly. "Brag Spotter wrote this row" is encoded
    in `agent_run_id` — JOIN to `agent_audit_log.events` where
    `agent_id='brag-spotter'`. Mirrors Reflection v2's write convention
    (`source_kind='reflection'` for reflection-anchored wins).
    """
    return {
        "win_id": win_id,
        "captured_at": now.isoformat(),
        "week_of": week_of.isoformat(),
        "source_kind": candidate.source_kind,
        "source_id": candidate.source_id,
        "title": candidate.title,
        "summary": candidate.summary,
        "evidence_links": list(candidate.evidence_links),
        "agent_run_id": agent_run_id,
    }


def _row_exists(
    bq_query: BQQueryClient,
    *,
    table_ref: str,
    win_id: str,
) -> bool:
    """Pre-INSERT dedup SELECT — column whitelisted to `win_id`."""
    sql = (
        f"SELECT 1 FROM `{table_ref}` "  # noqa: S608 — column literal
        "WHERE win_id = @value LIMIT 1"
    )
    rows = bq_query.query_rows(
        sql,
        parameters=[{"name": "value", "type": "STRING", "value": win_id}],
    )
    return bool(rows)


class WinsWriter:
    """Inserts an `agent_outputs.wins` row, or no-ops on dedup hit."""

    def __init__(
        self,
        *,
        bq: BQRowsClient,
        bq_query: BQQueryClient,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "wins",
    ) -> None:
        self._bq = bq
        self._bq_query = bq_query
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"

    @property
    def table_ref(self) -> str:
        return self._table_ref

    def write(
        self,
        candidate: WinCandidate,
        *,
        week_of: date,
        agent_run_id: str | None,
        now: datetime | None = None,
    ) -> WriteOutcome:
        win_id = win_id_for(week_of=week_of, title=candidate.title)
        try:
            if _row_exists(self._bq_query, table_ref=self._table_ref, win_id=win_id):
                return WriteOutcome(win_id=win_id, written=False)
            row = build_win_row(
                candidate,
                win_id=win_id,
                week_of=week_of,
                agent_run_id=agent_run_id,
                now=now or datetime.now(UTC),
            )
            errors = self._bq.insert_rows_json(self._table_ref, [row])
            if errors:
                raise RuntimeError(f"BQ rejected wins insert: {errors}")
            return WriteOutcome(win_id=win_id, written=True)
        except Exception as exc:
            log.exception("brag_spotter.wins_writer.write_failed win_id=%s", win_id)
            return WriteOutcome(
                win_id=win_id,
                written=False,
                error=f"{type(exc).__name__}: {exc}",
            )
