"""Morning Brief BQ writer with per-(recipient, local_date) dedup.

ADR 0029 §4: pre-INSERT SELECT on `agent_outputs.morning_briefs` matching
`(recipient_email, local_date)`. Hit → skip the Gmail draft + INSERT and
mark `dedup_skipped=True`. Miss → INSERT one row.

Mirrors the ADR 0026 idiom (Triage `find_recent_by_hash`) but the dedup
key is the (recipient, date) pair instead of a content hash. Streaming
buffer constraints don't apply: this is SELECT (not DML), and the row
volume is one per recipient per day.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from .models import MorningBriefOutput


class BQRowsClient(Protocol):
    """Matches `google.cloud.bigquery.Client.insert_rows_json`."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQDedupClient(Protocol):
    """Parameterized SELECT for the per-day dedup pre-check."""

    def find_existing_brief_id(
        self, table_ref: str, recipient_email: str, local_date: date
    ) -> str | None: ...


class MorningBriefWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class MorningBriefRow:
    """One row in `agent_outputs.morning_briefs`."""

    brief_id: str
    agent_run_id: str
    recipient_email: str
    local_date: str  # ISO date
    generated_at: str  # ISO 8601 UTC
    body_markdown: str
    sections_used: tuple[str, ...]
    prompt_version: str
    model: str
    latency_ms: int
    success: bool
    gmail_draft_id: str | None = None
    dedup_skipped: bool = False
    dedup_existing_brief_id: str | None = None
    error: str | None = None

    def to_bq_row(self) -> dict:
        return {
            "brief_id": self.brief_id,
            "agent_run_id": self.agent_run_id,
            "recipient_email": self.recipient_email,
            "local_date": self.local_date,
            "generated_at": self.generated_at,
            "body_markdown": self.body_markdown,
            "sections_used": list(self.sections_used),
            "gmail_draft_id": self.gmail_draft_id,
            "dedup_skipped": self.dedup_skipped,
            "dedup_existing_brief_id": self.dedup_existing_brief_id,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error": self.error,
        }


class MorningBriefWriter:
    """Streaming insert into `agent_outputs.morning_briefs` with dedup."""

    def __init__(
        self,
        *,
        bq_client: BQRowsClient,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "morning_briefs",
        dedup_client: BQDedupClient | None = None,
    ) -> None:
        self._bq = bq_client
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"
        self._dedup = dedup_client

    def find_existing(self, recipient_email: str, local_date: date) -> str | None:
        """Return existing `brief_id` for (recipient, local_date), or None.

        ADR 0029: dedup pre-check before the agent invokes the LLM /
        drafts a Gmail. Returns None when no dedup client is configured
        (test path) or no match exists.
        """
        if self._dedup is None:
            return None
        return self._dedup.find_existing_brief_id(self._table_ref, recipient_email, local_date)

    def write(
        self,
        *,
        output: MorningBriefOutput,
        run_id: str,
        prompt_version: str,
        model: str,
        latency_ms: int,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Insert one row reflecting `output` + run metadata."""
        row = MorningBriefRow(
            brief_id=output.brief_id,
            agent_run_id=run_id,
            recipient_email=output.recipient_email,
            local_date=output.local_date.isoformat(),
            generated_at=datetime.now(UTC).isoformat(),
            body_markdown=output.body_markdown,
            sections_used=output.sections_used,
            prompt_version=prompt_version,
            model=model,
            latency_ms=latency_ms,
            success=success,
            gmail_draft_id=output.gmail_draft_id,
            dedup_skipped=output.dedup_skipped,
            dedup_existing_brief_id=output.dedup_existing_brief_id,
            error=error,
        )
        errors = self._bq.insert_rows_json(self._table_ref, [row.to_bq_row()])
        if errors:
            raise MorningBriefWriteError(f"BQ rejected morning_briefs insert: {errors}")


def new_brief_id() -> str:
    return str(uuid.uuid4())
