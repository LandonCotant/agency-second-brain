"""Evening Reflection BQ writer with per-(recipient, local_date, mode) dedup (ADRs 0036, 0040).

Pattern mirrors ``agents/morning_brief/writer.py``. Pre-INSERT SELECT on
``agent_outputs.evening_reflections`` matching ``(recipient_email,
local_date, mode)`` AND ``success = TRUE`` AND ``gmail_draft_id IS NOT NULL``.
Hit → skip the Gmail draft + INSERT and mark ``dedup_skipped=True``.
Miss → INSERT one row.

ADR 0040 §7 — ``mode`` was added in PR-C. Pre-PR-C rows have NULL mode
and are treated as ``reflect`` for dedup purposes (the only mode that
existed before). The dedup query handles both shapes via
``mode = @mode OR (mode IS NULL AND @mode = 'reflect')`` so a 21:00 PT
reflect tick still collides with yesterday's pre-PR-C reflect row.

Streaming-buffer constraints don't apply: this is SELECT (not DML), and
the row volume is one per recipient per day per mode.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from .models import EveningReflectionOutput


class BQRowsClient(Protocol):
    """Matches ``google.cloud.bigquery.Client.insert_rows_json``."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQDedupClient(Protocol):
    """Parameterized SELECT for the per-day-per-mode dedup pre-check."""

    def find_existing_reflection_id(
        self,
        table_ref: str,
        recipient_email: str,
        local_date: date,
        mode: str = "reflect",
    ) -> str | None: ...


class EveningReflectionWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class EveningReflectionRow:
    """One row in ``agent_outputs.evening_reflections``."""

    reflection_id: str
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
    mode: str = "reflect"
    """ADR 0040 §7 — prompt | reflect. Defaults to reflect so v1 callers
    that don't thread mode keep the v1 dedup semantics."""
    gmail_draft_id: str | None = None
    dedup_skipped: bool = False
    dedup_existing_reflection_id: str | None = None
    error: str | None = None
    reflection_doc_id: str | None = None
    """ADR 0044 — REFLECT-mode artifact id. NULL on PROMPT-mode rows
    (PROMPT keeps gmail_draft_id) and on REFLECT rows that fell back to
    the Gmail-draft path."""
    reflection_doc_url: str | None = None
    """Drive ``webViewLink`` for the Reflection Doc."""

    def to_bq_row(self) -> dict:
        return {
            "reflection_id": self.reflection_id,
            "agent_run_id": self.agent_run_id,
            "recipient_email": self.recipient_email,
            "local_date": self.local_date,
            "generated_at": self.generated_at,
            "body_markdown": self.body_markdown,
            "sections_used": list(self.sections_used),
            "gmail_draft_id": self.gmail_draft_id,
            "dedup_skipped": self.dedup_skipped,
            "dedup_existing_reflection_id": self.dedup_existing_reflection_id,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error": self.error,
            "mode": self.mode,
            "reflection_doc_id": self.reflection_doc_id,
            "reflection_doc_url": self.reflection_doc_url,
        }


class EveningReflectionWriter:
    """Streaming insert into ``agent_outputs.evening_reflections`` with dedup."""

    def __init__(
        self,
        *,
        bq_client: BQRowsClient,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "evening_reflections",
        dedup_client: BQDedupClient | None = None,
    ) -> None:
        self._bq = bq_client
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"
        self._dedup = dedup_client

    def find_existing(
        self,
        recipient_email: str,
        local_date: date,
        mode: str = "reflect",
    ) -> str | None:
        """Return existing ``reflection_id`` for (recipient, local_date, mode), or None.

        Returns None when no dedup client is configured (test path) or
        no successful reflection with a Gmail draft exists for the day
        in the given mode.
        """
        if self._dedup is None:
            return None
        return self._dedup.find_existing_reflection_id(
            self._table_ref, recipient_email, local_date, mode
        )

    def write(
        self,
        *,
        output: EveningReflectionOutput,
        run_id: str,
        prompt_version: str,
        model: str,
        latency_ms: int,
        success: bool,
        error: str | None = None,
        mode: str = "reflect",
    ) -> None:
        """Insert one row reflecting ``output`` + run metadata."""
        row = EveningReflectionRow(
            reflection_id=output.reflection_id,
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
            dedup_existing_reflection_id=output.dedup_existing_reflection_id,
            error=error,
            mode=mode,
            reflection_doc_id=output.reflection_doc_id,
            reflection_doc_url=output.reflection_doc_url,
        )
        errors = self._bq.insert_rows_json(self._table_ref, [row.to_bq_row()])
        if errors:
            raise EveningReflectionWriteError(f"BQ rejected evening_reflections insert: {errors}")


def new_reflection_id() -> str:
    return str(uuid.uuid4())
