"""Cloud Run Job entrypoint for WS-G PKM Phase 3 — Brag Spotter (ADR 0043).

Run shape (one Cloud Run Job execution per scheduler tick):
  1. For each recipient in `BRAG_SPOTTER_RECIPIENTS`, construct a
     `BragSpotterAgent`, call `invoke()` to scan + compose + write
     wins + dispatch the digest.
  2. Catch-and-continue per recipient — one bad calendar fetch
     shouldn't block the whole batch (v1 has a single recipient,
     but the loop is here for symmetry with Morning Brief).

ADR 0043 §1: Vertex SDK direct (`gemini-2.5-flash`), NOT a Reasoning
Engine.

Required env vars:
  - BRAIN_PROJECT_ID
  - BRAG_SPOTTER_RECIPIENTS (comma-separated; default owner@example.com)
  - BRAG_SPOTTER_TIMEZONE   (default America/Los_Angeles)
  - TRIAGE_SA_EMAIL         (impersonation source for gmail.compose; defaults to asb-agent-triage-sa@<proj>)
  - LOOKBACK_DAYS           (default 7)
  - CHAT_WEBHOOK_SECRET_ID  (default second-brain-gchat-webhook; empty disables Chat)
  - CHAT_WEBHOOK_SECRET_VERSION (default 'latest')
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("agency_brain.agents.brag_spotter.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    recipients_raw = os.environ.get("BRAG_SPOTTER_RECIPIENTS", "owner@example.com")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    tz_name = os.environ.get("BRAG_SPOTTER_TIMEZONE", "America/Los_Angeles")
    sa_email = os.environ.get(
        "TRIAGE_SA_EMAIL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    chat_secret_id = os.environ.get("CHAT_WEBHOOK_SECRET_ID", "second-brain-gchat-webhook")
    chat_secret_version = os.environ.get("CHAT_WEBHOOK_SECRET_VERSION", "latest")

    try:
        local_today = _local_today(tz_name)
    except Exception:
        log.exception("brag_spotter.main: bad timezone %r — using UTC", tz_name)
        local_today = datetime.utcnow().date()
    week_of = _monday_of_iso_week(local_today)

    log.info(
        "brag_spotter.start: recipients=%s run_date=%s week_of=%s lookback_days=%d",
        recipients,
        local_today.isoformat(),
        week_of.isoformat(),
        lookback_days,
    )

    # Lazy imports — Cloud Run Job cold-start cost grows with import time.
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.dwd import DWDServiceFactory
    from ...common.memory_bank import InMemoryMemoryBank
    from ...common.prompts import load_prompt
    from ...routing.channels.chat import ChatWebhookClient, UrllibPoster
    from ..morning_brief.gmail_drafts_client import GmailDraftsClient
    from .agent import BragSpotterAgent
    from .composer import (
        DEFAULT_MODEL,
        BragSpotterComposeConfig,
        BragSpotterComposer,
        VertexBragSpotterComposeClient,
    )
    from .digest import DigestDispatcher
    from .models import BragSpotterInput
    from .readers import (
        ExistingWinsForWeekReader,
        RecentDecisionsReader,
        RecentNotesReader,
        RecentReflectionsReader,
        RecentRoutedEventsReader,
        RecentTriagedItemsReader,
    )
    from .writer import WinsWriter

    bq_client = bigquery.Client(project=project_id)
    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)

    bq_rows = _BigQueryRowsAdapter(bq_client)
    bq_query_paramed = _BigQueryParamQueryAdapter(bq_client)

    # Composer + LLM
    structured_llm = VertexBragSpotterComposeClient(BragSpotterComposeConfig(project_id=project_id))
    composer = BragSpotterComposer(
        prompt_template=load_prompt("brag_spotter", "v1"),
        llm=structured_llm,
        model=DEFAULT_MODEL,
    )

    # Readers
    triaged_reader = RecentTriagedItemsReader(
        bq_client=bq_rows, project_id=project_id, lookback_days=lookback_days
    )
    routed_reader = RecentRoutedEventsReader(
        bq_client=bq_rows, project_id=project_id, lookback_days=lookback_days
    )
    notes_reader = RecentNotesReader(
        bq_client=bq_rows, project_id=project_id, lookback_days=lookback_days
    )
    decisions_reader = RecentDecisionsReader(
        bq_client=bq_rows, project_id=project_id, lookback_days=lookback_days
    )
    reflections_reader = RecentReflectionsReader(
        bq_client=bq_rows, project_id=project_id, lookback_days=lookback_days
    )
    existing_wins_reader = ExistingWinsForWeekReader(bq_client=bq_rows, project_id=project_id)

    # Writer
    wins_writer = WinsWriter(
        bq=bq_client,
        bq_query=bq_query_paramed,
        project_id=project_id,
    )

    # Dispatch — Chat (optional, secret-backed) + Gmail draft (DWD)
    chat_client: ChatWebhookClient | None = None
    if chat_secret_id:
        try:
            from ...routing.fanout_main import load_webhook_url

            webhook_url = load_webhook_url(
                project_id=project_id,
                secret_id=chat_secret_id,
                version=chat_secret_version,
            )
            chat_client = ChatWebhookClient(webhook_url, http=UrllibPoster())
        except Exception:
            log.exception("brag_spotter.main: failed to build Chat client; continuing without Chat")

    gmail_client = GmailDraftsClient(
        service_factory=DWDServiceFactory(
            target_principal=sa_email,
            scope="https://www.googleapis.com/auth/gmail.compose",
            api="gmail",
            api_version="v1",
        )
    )
    dispatcher = DigestDispatcher(chat=chat_client, gmail=gmail_client)

    failures = 0
    for recipient in recipients:
        agent = BragSpotterAgent(
            sa_email=sa_email,
            audit_log=audit,
            memory_bank=InMemoryMemoryBank(),
            composer=composer,
            triaged_items_reader=triaged_reader,
            routed_events_reader=routed_reader,
            notes_reader=notes_reader,
            decisions_reader=decisions_reader,
            reflections_reader=reflections_reader,
            existing_wins_reader=existing_wins_reader,
            wins_writer=wins_writer,
            dispatcher=dispatcher,
        )
        input_ = BragSpotterInput(
            recipient_email=recipient,
            run_date=local_today,
            week_of=week_of,
        )

        started = time.perf_counter()
        try:
            agent.invoke(input_)
        except Exception:
            failures += 1
            log.exception("brag_spotter.recipient_failed recipient=%s", recipient)
            continue
        latency_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "brag_spotter.recipient_done recipient=%s latency_ms=%d",
            recipient,
            latency_ms,
        )

    log.info(
        "brag_spotter.done recipients=%d failures=%d week_of=%s",
        len(recipients),
        failures,
        week_of.isoformat(),
    )
    return 0 if failures == 0 else 1


def _local_today(tz_name: str) -> date:
    return datetime.now(tz=ZoneInfo(tz_name)).date()


def _monday_of_iso_week(d: date) -> date:
    """ISO Monday of the week containing `d`. Mirrors Reflection v2's helper."""
    return d - timedelta(days=d.weekday())


# ----------------------------------------------------- adapters


class _BigQueryRowsAdapter:
    """Tiny adapter matching `readers.BQQueryClient` (single-arg query_rows)."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str) -> list[dict]:
        return [dict(row.items()) for row in self._bq.query(sql).result()]


class _BigQueryParamQueryAdapter:
    """Adapter matching `writer.BQQueryClient` — parameterized SELECT.

    Mirrors the shape `evening_reflection.extracted_writers.BQQueryClient`
    expects.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery

        params: list[Any] = []
        for p in parameters or []:
            params.append(bigquery.ScalarQueryParameter(p["name"], p["type"], p["value"]))
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


if __name__ == "__main__":
    sys.exit(main())
