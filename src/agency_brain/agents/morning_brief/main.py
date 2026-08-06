"""Cloud Run Job entrypoint for the WS-G3 Morning Brief.

Run shape (one Cloud Run Job execution per scheduler tick):
  1. For each recipient in `BRIEF_RECIPIENTS`, construct a
     `MorningBriefAgent`, call `invoke()` to produce a brief, then
     write a row to `agent_outputs.morning_briefs`.
  2. Catch-and-continue per recipient — one bad calendar fetch
     shouldn't block the whole batch.

ADR 0029 §3: Vertex SDK direct (`gemini-2.5-flash`), NOT a Reasoning
Engine.

Required env vars:
  - BRAIN_PROJECT_ID
  - BRIEF_RECIPIENTS (comma-separated)
  - BRIEF_TIMEZONE  (default America/Los_Angeles)
  - TRIAGE_SA_EMAIL (the SA the Cloud Run Job runs as; used for DWD
    impersonation source)
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("agency_brain.agents.morning_brief.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    recipients_raw = os.environ.get("BRIEF_RECIPIENTS", "owner@example.com")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    tz_name = os.environ.get("BRIEF_TIMEZONE", "America/Los_Angeles")
    sa_email = os.environ.get(
        "TRIAGE_SA_EMAIL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )

    try:
        local_today = _local_today(tz_name)
    except Exception:
        log.exception("morning_brief.main: bad timezone %r — using UTC", tz_name)
        local_today = datetime.utcnow().date()

    log.info(
        "morning_brief.start: recipients=%s local_date=%s",
        recipients,
        local_today.isoformat(),
    )

    # Lazy imports — Cloud Run Jobs cold-start cost grows with import time.
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.dwd import DWDServiceFactory
    from ...common.memory_bank import InMemoryMemoryBank
    from ...common.prompts import load_prompt
    from .agent import MorningBriefAgent
    from .calendar_client import CalendarClient
    from .composer import DEFAULT_MODEL, MorningBriefComposer
    from .gmail_drafts_client import GmailDraftsClient
    from .readers import (
        DraftsAwaitingReviewReader,
        OpenTasksForOwnerReader,
        RiskFlagsReader,
        TriagedItemsForOwnerReader,
    )
    from .writer import MorningBriefWriter

    bq_client = bigquery.Client(project=project_id)
    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)

    bq_rows = _BigQueryRowsAdapter(bq_client)
    bq_dedup = _BigQueryDedupAdapter(bq_client)

    composer = MorningBriefComposer(
        prompt_template=load_prompt("morning_brief", "v1"),
        llm=_VertexComposeClient(project_id=project_id, location="us-central1"),
        model=DEFAULT_MODEL,
    )
    triaged_items_reader = TriagedItemsForOwnerReader(bq_client=bq_rows, project_id=project_id)
    open_tasks_reader = OpenTasksForOwnerReader(bq_client=bq_rows, project_id=project_id)
    risk_flags_reader = RiskFlagsReader(bq_client=bq_rows, project_id=project_id)
    drafts_awaiting_reader = DraftsAwaitingReviewReader(bq_client=bq_rows, project_id=project_id)
    calendar_client = CalendarClient(
        service_factory=DWDServiceFactory(
            target_principal=sa_email,
            scope="https://www.googleapis.com/auth/calendar.readonly",
            api="calendar",
            api_version="v3",
        )
    )
    gmail_drafts_client = GmailDraftsClient(
        service_factory=DWDServiceFactory(
            target_principal=sa_email,
            scope="https://www.googleapis.com/auth/gmail.compose",
            api="gmail",
            api_version="v1",
        )
    )
    writer = MorningBriefWriter(
        bq_client=bq_client,
        project_id=project_id,
        dedup_client=bq_dedup,
    )

    failures = 0
    for recipient in recipients:
        run_id = str(uuid.uuid4())
        agent = MorningBriefAgent(
            sa_email=sa_email,
            audit_log=audit,
            memory_bank=InMemoryMemoryBank(),
            composer=composer,
            triaged_items_reader=triaged_items_reader,
            open_tasks_reader=open_tasks_reader,
            risk_flags_reader=risk_flags_reader,
            drafts_awaiting_reader=drafts_awaiting_reader,
            calendar_client=calendar_client,
            gmail_drafts_client=gmail_drafts_client,
            writer=writer,
            timezone=tz_name,
        )
        from .models import MorningBriefInput

        input_ = MorningBriefInput(
            recipient_email=recipient,
            run_date=local_today,
        )

        started = time.perf_counter()
        try:
            output = agent.invoke(input_)
        except Exception as exc:
            failures += 1
            log.exception("morning_brief.recipient_failed recipient=%s", recipient)
            # Best-effort failure row so the BQ table reflects every attempt.
            try:
                from .models import MorningBriefOutput

                fail_output = MorningBriefOutput(
                    brief_id=str(uuid.uuid4()),
                    recipient_email=recipient,
                    local_date=local_today,
                    body_markdown="(failed before composition)",
                    sections_used=(),
                    prompt_version="v1",
                )
                writer.write(
                    output=fail_output,
                    run_id=run_id,
                    prompt_version="v1",
                    model=DEFAULT_MODEL,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                log.exception("morning_brief.failure_row_write_failed recipient=%s", recipient)
            continue

        latency_ms = int((time.perf_counter() - started) * 1000)
        # ADR 0029: dedup-skip rows are NOT re-inserted (mirrors ADR 0026
        # where dedup-hits emit the audit row but skip the BQ output).
        if output.dedup_skipped:
            log.info(
                "morning_brief.dedup_skip recipient=%s existing=%s latency_ms=%d",
                recipient,
                output.dedup_existing_brief_id,
                latency_ms,
            )
            continue

        try:
            writer.write(
                output=output,
                run_id=run_id,
                prompt_version="v1",
                model=DEFAULT_MODEL,
                latency_ms=latency_ms,
                success=True,
            )
        except Exception:
            failures += 1
            log.exception("morning_brief.bq_write_failed recipient=%s", recipient)

    log.info(
        "morning_brief.done recipients=%d failures=%d",
        len(recipients),
        failures,
    )
    return 0 if failures == 0 else 1


def _local_today(tz_name: str) -> date:
    return datetime.now(tz=ZoneInfo(tz_name)).date()


# ----------------------------------------------------- adapters


class _BigQueryRowsAdapter:
    """Tiny adapter matching readers.BQQueryClient."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str) -> list[dict]:
        return [dict(row.items()) for row in self._bq.query(sql).result()]


class _BigQueryDedupAdapter:
    """Implements writer.BQDedupClient via parameterized SELECT."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def find_existing_brief_id(
        self, table_ref: str, recipient_email: str, local_date: date
    ) -> str | None:
        from google.cloud import bigquery

        # Dedup only on rows that actually produced a Gmail draft. Failure
        # markers (success=false, gmail_draft_id IS NULL) MUST NOT block a
        # retry — that's exactly the case where we want to try again.
        sql = (
            f"SELECT brief_id FROM `{table_ref}` "  # noqa: S608
            "WHERE recipient_email = @recipient "
            "AND local_date = @local_date "
            "AND success = TRUE "
            "AND gmail_draft_id IS NOT NULL "
            "ORDER BY generated_at DESC LIMIT 1"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("recipient", "STRING", recipient_email),
                bigquery.ScalarQueryParameter("local_date", "DATE", local_date.isoformat()),
            ]
        )
        rows = list(self._bq.query(sql, job_config=job_config).result())
        if not rows:
            return None
        return rows[0]["brief_id"]


class _VertexComposeClient:
    """Calls Vertex AI gemini-2.5-flash directly (no Reasoning Engine).

    Per ADR 0029 §3: Vertex SDK direct, NOT a Reasoning Engine. Avoids
    the orphan-RE cost incident posture (PR #56 / ADR 0028). Uses the
    ``google.genai`` SDK in Vertex backend mode (``Client(vertexai=True)``)
    per the F4 migration — the legacy ``vertexai.generative_models`` module
    reaches EOL 2026-06-24.
    """

    def __init__(self, *, project_id: str, location: str) -> None:
        self._project_id = project_id
        self._location = location
        self._client = None

    def compose(self, *, prompt: str, model: str) -> str:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=self._project_id,
                location=self._location,
            )
        response = self._client.models.generate_content(model=model, contents=prompt)
        return getattr(response, "text", "") or ""


if __name__ == "__main__":
    sys.exit(main())
