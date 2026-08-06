"""Cloud Run Job entrypoint for the WS-G4 Evening Reflection (ADRs 0036, 0040).

Run shape (one Cloud Run Job execution per scheduler tick):
  1. For each recipient in ``REFLECTION_RECIPIENTS``, construct an
     ``EveningReflectionAgent``, call ``invoke()`` to produce a
     reflection, then write a row to
     ``agent_outputs.evening_reflections``.
  2. Catch-and-continue per recipient — one bad calendar fetch
     shouldn't block the whole batch.

ADR 0036 §2: Vertex SDK direct (``gemini-2.5-flash``), NOT a Reasoning
Engine. Mirrors ``agents/morning_brief/main.py`` end-to-end.

ADR 0040 §1: ``REFLECTION_MODE`` (``prompt`` | ``reflect``, default
``reflect``) selects the dispatch. Both modes share this entrypoint and
the same Cloud Run Job; PR-C wires two Cloud Schedulers that pass
``containerOverrides.env.REFLECTION_MODE`` to fork behavior.

Required env vars:
  - BRAIN_PROJECT_ID
  - REFLECTION_MODE      (prompt | reflect; default reflect — ADR 0040)
  - REFLECTION_RECIPIENTS (comma-separated; default owner@example.com)
  - REFLECTION_TIMEZONE  (default America/Los_Angeles)
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

from .models import ReflectionMode

log = logging.getLogger("agency_brain.agents.evening_reflection.main")


def resolve_mode(env_value: str | None) -> ReflectionMode:
    """Map a ``REFLECTION_MODE`` env value to ``ReflectionMode`` (ADR 0040 §1).

    Anything other than the literal ``"prompt"`` (case-insensitive,
    whitespace-trimmed) resolves to ``REFLECT``. This intentionally
    fail-closes to the safe default — REFLECT — so a typo in the
    scheduler containerOverrides body doesn't accidentally swap modes.
    """
    if env_value is None:
        return ReflectionMode.REFLECT
    cleaned = env_value.strip().lower()
    if cleaned == "prompt":
        return ReflectionMode.PROMPT
    return ReflectionMode.REFLECT


def resolve_prompt_version(mode: ReflectionMode) -> str:
    """Map a mode to the prompt-template version string (ADR 0040 §1).

    The version string is what ``load_prompt("evening_reflection", v)``
    consumes — it must match a file under
    ``prompts/evening_reflection/<v>.md``. ADR 0044 — REFLECT mode now
    loads ``reflect_doc_v1`` so the LLM emits ``custom_questions``
    alongside the existing extraction arrays.
    """
    if mode is ReflectionMode.PROMPT:
        return "prompt_v1"
    return "reflect_doc_v1"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    recipients_raw = os.environ.get("REFLECTION_RECIPIENTS", "owner@example.com")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    tz_name = os.environ.get("REFLECTION_TIMEZONE", "America/Los_Angeles")
    sa_email = os.environ.get(
        "TRIAGE_SA_EMAIL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )

    # ADR 0040 §1 — REFLECTION_MODE selects PROMPT (16:00 PT anchor) or
    # REFLECT (21:00 PT reflection). Default is reflect so v1 callers
    # / fallback runs (no override) preserve ADR 0036 behavior.
    mode = resolve_mode(os.environ.get("REFLECTION_MODE"))
    prompt_version = resolve_prompt_version(mode)

    try:
        local_today = _local_today(tz_name)
    except Exception:
        log.exception("evening_reflection.main: bad timezone %r — using UTC", tz_name)
        local_today = datetime.utcnow().date()

    log.info(
        "evening_reflection.start: mode=%s recipients=%s local_date=%s",
        mode.value,
        recipients,
        local_today.isoformat(),
    )

    # Lazy imports — Cloud Run Jobs cold-start cost grows with import time.
    from google.cloud import bigquery, pubsub_v1

    from ...common.audit_log import AuditLogClient
    from ...common.drive_doc_writer import (
        ADCDriveServiceFactory,
        DriveDocClient,
    )
    from ...common.dwd import DWDServiceFactory
    from ...common.memory_bank import InMemoryMemoryBank
    from ...common.prompts import load_prompt
    from ...common.standard_questions import (
        StandardQuestionsLoadError,
        load_standard_questions,
    )
    from ...routing.channels.chat import ChatWebhookClient, UrllibPoster
    from ..morning_brief.calendar_client import CalendarClient
    from ..morning_brief.gmail_drafts_client import GmailDraftsClient
    from ..notes_ingestor.embedder import VertexEmbedder
    from .agent import EveningReflectionAgent
    from .areas_context_reader import AreasContextReader
    from .composer import DEFAULT_MODEL, EveningReflectionComposer
    from .extracted_writers import DecisionsWriter, WinsWriter
    from .readers import (
        ActiveRiskFlagsTodayReader,
        CompletedTasksTodayReader,
        InFlightDecisionsReader,
        MorningBriefForTodayReader,
        RecentVoiceMemosReader,
        TriagedItemsTodayReader,
    )
    from .reflection_doc_writer import ReflectionDocWriter
    from .structured_compose import (
        StructuredComposeConfig,
        VertexStructuredComposeClient,
    )
    from .triage_publisher import ReflectTriagePublisher
    from .writer import EveningReflectionWriter

    bq_client = bigquery.Client(project=project_id)
    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)

    bq_rows = _BigQueryRowsAdapter(bq_client)
    bq_dedup = _BigQueryDedupAdapter(bq_client)

    # ADR 0040 §5: REFLECT mode also constructs a structured-compose client
    # (google.genai + response_schema) for decision/win/todo extraction.
    # PROMPT mode skips this — prose-only output, no extraction.
    structured_llm = (
        VertexStructuredComposeClient(
            StructuredComposeConfig(project_id=project_id, location="us-central1")
        )
        if mode is ReflectionMode.REFLECT
        else None
    )
    composer = EveningReflectionComposer(
        prompt_template=load_prompt("evening_reflection", prompt_version),
        llm=_VertexComposeClient(project_id=project_id, location="us-central1"),
        model=DEFAULT_MODEL,
        structured_llm=structured_llm,
    )
    completed_tasks_reader = CompletedTasksTodayReader(bq_client=bq_rows, project_id=project_id)
    triaged_today_reader = TriagedItemsTodayReader(
        bq_client=bq_rows, project_id=project_id, timezone=tz_name
    )
    morning_brief_reader = MorningBriefForTodayReader(bq_client=bq_rows, project_id=project_id)
    active_risk_flags_reader = ActiveRiskFlagsTodayReader(
        bq_client=bq_rows, project_id=project_id, timezone=tz_name
    )
    # ADR 0040 §1 — only the mode that needs each reader instantiates it.
    # Leaving the unused reader as None keeps cold-start light and makes
    # the dispatch in agent.py explicit.
    voice_memos_reader = (
        RecentVoiceMemosReader(bq_client=bq_rows, project_id=project_id)
        if mode is ReflectionMode.REFLECT
        else None
    )
    in_flight_decisions_reader = (
        InFlightDecisionsReader(bq_client=bq_rows, project_id=project_id)
        if mode is ReflectionMode.PROMPT
        else None
    )
    # ADR 0040 §6 — REFLECT-mode dispatch surfaces. ``decisions`` and
    # ``wins`` are streaming-insert tables (mirror captures-materializer's
    # pattern). The triage publisher targets the existing asb-triage-input
    # topic so todo dispatches reuse the Triage classifier (no new topic).
    decisions_writer: DecisionsWriter | None = None
    wins_writer: WinsWriter | None = None
    triage_publisher: ReflectTriagePublisher | None = None
    if mode is ReflectionMode.REFLECT:
        bq_param = _BigQueryParameterizedAdapter(bq_client)
        bq_writer_adapter = _BigQueryInsertAdapter(bq_client)
        decisions_writer = DecisionsWriter(
            bq=bq_writer_adapter,
            bq_query=bq_param,
            table_ref=f"{project_id}.agent_outputs.decisions",
        )
        wins_writer = WinsWriter(
            bq=bq_writer_adapter,
            bq_query=bq_param,
            table_ref=f"{project_id}.agent_outputs.wins",
        )
        triage_topic_path = (
            os.environ.get("TRIAGE_INPUT_TOPIC") or f"projects/{project_id}/topics/asb-triage-input"
        )
        publisher_client = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        triage_publisher = ReflectTriagePublisher(
            publisher=publisher_client,
            topic_path=triage_topic_path,
            sender_email=recipients[0] if recipients else "owner@example.com",
        )
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
    writer = EveningReflectionWriter(
        bq_client=bq_client,
        project_id=project_id,
        dedup_client=bq_dedup,
    )

    # ADR 0044 — REFLECT mode artifact is a Google Doc in Brain/Areas/Reflections/.
    # Wired only when the parent folder id is set in env vars (lets PROMPT-only
    # ticks and tests skip Drive setup entirely). PROMPT mode never uses these.
    reflection_doc_writer: ReflectionDocWriter | None = None
    chat_client: ChatWebhookClient | None = None
    standard_questions = None
    if mode is ReflectionMode.REFLECT:
        reflections_folder_id = os.environ.get("BRAIN_AREAS_REFLECTIONS_FOLDER_ID", "").strip()
        if reflections_folder_id:
            try:
                drive_doc_client = DriveDocClient(service_factory=ADCDriveServiceFactory())
                reflection_doc_writer = ReflectionDocWriter(
                    drive_client=drive_doc_client,
                    parent_folder_id=reflections_folder_id,
                )
            except Exception:
                log.exception("evening_reflection.reflection_doc_writer_init_failed")
                reflection_doc_writer = None
        else:
            log.info(
                "evening_reflection.reflection_doc_writer_disabled: "
                "BRAIN_AREAS_REFLECTIONS_FOLDER_ID is empty (REFLECT mode falls back to Gmail draft)"
            )

        chat_webhook_url = os.environ.get("BRAIN_ALERTS_CHAT_WEBHOOK_URL", "").strip()
        if chat_webhook_url:
            try:
                chat_client = ChatWebhookClient(chat_webhook_url, http=UrllibPoster())
            except Exception:
                log.exception("evening_reflection.chat_client_init_failed")
                chat_client = None

        try:
            standard_questions = load_standard_questions("evening_reflection")
        except StandardQuestionsLoadError:
            log.exception("evening_reflection.standard_questions_load_failed")
            standard_questions = None

    # ADR 0038 §5 + ADR 0044 — REFLECT mode optionally pulls top-K
    # Areas/Resources notes via VECTOR_SEARCH for the Doc body's
    # "Areas context" section. Disabled when REFLECTION_AREAS_TOP_K=0
    # or when the Doc surface itself is disabled (no folder share).
    areas_context_reader: AreasContextReader | None = None
    if mode is ReflectionMode.REFLECT and reflection_doc_writer is not None:
        try:
            top_k = int(os.environ.get("REFLECTION_AREAS_TOP_K", "3") or "0")
        except ValueError:
            top_k = 3
        if top_k > 0:
            try:
                embedder = VertexEmbedder(project_id=project_id, location="us-central1")
                areas_context_reader = AreasContextReader(
                    bq_client=bq_param,
                    embedder=embedder,
                    project_id=project_id,
                    top_k=top_k,
                )
            except Exception:
                log.exception("evening_reflection.areas_context_reader_init_failed")
                areas_context_reader = None
        else:
            log.info("evening_reflection.areas_context_reader_disabled: REFLECTION_AREAS_TOP_K=0")

    failures = 0
    for recipient in recipients:
        run_id = str(uuid.uuid4())
        agent = EveningReflectionAgent(
            sa_email=sa_email,
            audit_log=audit,
            memory_bank=InMemoryMemoryBank(),
            composer=composer,
            completed_tasks_reader=completed_tasks_reader,
            triaged_today_reader=triaged_today_reader,
            morning_brief_reader=morning_brief_reader,
            active_risk_flags_reader=active_risk_flags_reader,
            calendar_client=calendar_client,
            gmail_drafts_client=gmail_drafts_client,
            writer=writer,
            timezone=tz_name,
            voice_memos_reader=voice_memos_reader,
            in_flight_decisions_reader=in_flight_decisions_reader,
            decisions_writer=decisions_writer,
            wins_writer=wins_writer,
            triage_publisher=triage_publisher,
            prompt_version=prompt_version,
            reflection_doc_writer=reflection_doc_writer,
            standard_questions=standard_questions,
            chat_client=chat_client,
            areas_context_reader=areas_context_reader,
        )
        from .models import EveningReflectionInput

        input_ = EveningReflectionInput(
            recipient_email=recipient,
            run_date=local_today,
            mode=mode,
        )

        started = time.perf_counter()
        try:
            output = agent.invoke(input_)
        except Exception as exc:
            failures += 1
            log.exception("evening_reflection.recipient_failed recipient=%s", recipient)
            try:
                from .models import EveningReflectionOutput

                fail_output = EveningReflectionOutput(
                    reflection_id=str(uuid.uuid4()),
                    recipient_email=recipient,
                    local_date=local_today,
                    body_markdown="(failed before composition)",
                    sections_used=(),
                    prompt_version=prompt_version,
                )
                writer.write(
                    output=fail_output,
                    run_id=run_id,
                    prompt_version=prompt_version,
                    model=DEFAULT_MODEL,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    mode=mode.value,
                )
            except Exception:
                log.exception(
                    "evening_reflection.failure_row_write_failed recipient=%s",
                    recipient,
                )
            continue

        latency_ms = int((time.perf_counter() - started) * 1000)
        if output.dedup_skipped:
            log.info(
                "evening_reflection.dedup_skip recipient=%s existing=%s latency_ms=%d",
                recipient,
                output.dedup_existing_reflection_id,
                latency_ms,
            )
            continue

        try:
            writer.write(
                output=output,
                run_id=run_id,
                prompt_version=prompt_version,
                model=DEFAULT_MODEL,
                latency_ms=latency_ms,
                success=True,
                mode=mode.value,
            )
        except Exception:
            failures += 1
            log.exception("evening_reflection.bq_write_failed recipient=%s", recipient)

    log.info(
        "evening_reflection.done recipients=%d failures=%d",
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

    def find_existing_reflection_id(
        self,
        table_ref: str,
        recipient_email: str,
        local_date: date,
        mode: str = "reflect",
    ) -> str | None:
        from google.cloud import bigquery

        # Dedup only on rows that produced a *real* artifact — Gmail
        # draft (PROMPT mode + REFLECT-fallback) OR Reflection Doc
        # (REFLECT mode post-ADR 0044). Failure markers (success=false
        # AND no artifact) MUST NOT block a retry — that's exactly the
        # case where we want to try again.
        # ADR 0040 §7 — mode-aware: pre-PR-C rows have mode IS NULL and
        # are treated as 'reflect' for dedup so today's 21:00 PT reflect
        # tick still collides with yesterday's pre-PR-C reflect row.
        sql = (
            f"SELECT reflection_id FROM `{table_ref}` "  # noqa: S608
            "WHERE recipient_email = @recipient "
            "AND local_date = @local_date "
            "AND success = TRUE "
            "AND (gmail_draft_id IS NOT NULL OR reflection_doc_id IS NOT NULL) "
            "AND (mode = @mode OR (mode IS NULL AND @mode = 'reflect')) "
            "ORDER BY generated_at DESC LIMIT 1"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("recipient", "STRING", recipient_email),
                bigquery.ScalarQueryParameter("local_date", "DATE", local_date.isoformat()),
                bigquery.ScalarQueryParameter("mode", "STRING", mode),
            ]
        )
        rows = list(self._bq.query(sql, job_config=job_config).result())
        if not rows:
            return None
        return rows[0]["reflection_id"]


class _BigQueryInsertAdapter:
    """Adapter from ``google.cloud.bigquery.Client`` to the writer's BQRowsClient.

    The writers want a ``insert_rows_json(table_ref, rows) -> errors`` shape
    (mirrors captures-materializer). Wraps the SDK call so writers stay
    SDK-agnostic + unit-testable with a fake.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._bq.insert_rows_json(table_ref, rows)


class _BigQueryParameterizedAdapter:
    """Adapter for the writer's BQQueryClient — parameterized SELECT.

    The dedup pre-check uses ``ScalarQueryParameter`` (not literal
    interpolation). Mirrors the captures-materializer's BQ adapter.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery as bq_module

        params = []
        for p in parameters or []:
            params.append(
                bq_module.ScalarQueryParameter(p["name"], p.get("type", "STRING"), p["value"])
            )
        job_config = bq_module.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


class _VertexComposeClient:
    """Calls Vertex AI gemini-2.5-flash directly (no Reasoning Engine).

    Per ADR 0036 §2: Vertex SDK direct, NOT a Reasoning Engine. Avoids
    the orphan-RE cost incident posture (PR #56 / ADR 0028). Uses the
    ``google.genai`` SDK in Vertex backend mode (``Client(vertexai=True)``)
    per the F4 migration — the legacy ``vertexai.generative_models`` module
    reaches EOL 2026-06-24. This is the PROMPT-mode prose path; REFLECT mode
    already runs on ``google.genai`` via ``VertexStructuredComposeClient``.
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
