"""WS-G4 Evening Reflection Agent (ADR 0036).

Mirrors ``agents/morning_brief/agent.py``: BaseAgent subclass; ``_run``
orchestrates dedup pre-check → 5 readers (graceful per-reader
degradation) → composer → Gmail draft → return. The Cloud Run Job
entrypoint (``main.py``) calls ``writer.write()`` AFTER ``invoke()``
returns so the BQ row reflects the dispatch outcome (latency_ms,
success, error).

Order of operations in ``_run``:
  1. Per-recipient-per-day dedup pre-check (writer.find_existing).
     Hit → return EveningReflectionOutput with dedup_skipped=True; no
     LLM, no Gmail draft.
  2. Read all five sources. Failures degrade gracefully — a missing
     Calendar response or empty Risk Watcher doesn't break the
     reflection; the section is just elided.
  3. Render section blocks + call LLM via composer.compose.
  4. Draft Gmail via gmail_drafts_client.draft.
  5. Return EveningReflectionOutput.
"""

from __future__ import annotations

import logging

from ...common.standard_questions import StandardQuestions
from ..base import BaseAgent
from ..morning_brief.calendar_client import CalendarClient
from ..morning_brief.gmail_drafts_client import GmailDraftsClient
from .areas_context_reader import (
    AreaNoteSnippet,
    AreasContextReader,
    build_theme_seeds,
)
from .chat_post import post_reflection_card
from .composer import (
    EveningReflectionComposer,
    render_reflection_doc_body_html,
    render_section_blocks,
)
from .extracted_writers import DecisionsWriter, WinsWriter
from .models import EveningReflectionInput, EveningReflectionOutput, ReflectionMode
from .readers import (
    ActiveRiskFlagsTodayReader,
    CompletedTasksTodayReader,
    InFlightDecisionsReader,
    MorningBriefForTodayReader,
    RecentVoiceMemosReader,
    TriagedItemsTodayReader,
)
from .reflect_dispatch import dispatch as reflect_dispatch
from .reflect_dispatch import render_dispatch_summary_block
from .reflection_doc_writer import ReflectionDocWriter
from .structured_compose import ParseError
from .triage_publisher import ReflectTriagePublisher
from .writer import EveningReflectionWriter, new_reflection_id

log = logging.getLogger("agency_brain.agents.evening_reflection.agent")


class EveningReflectionAgent(BaseAgent[EveningReflectionInput, EveningReflectionOutput]):
    def __init__(
        self,
        *,
        agent_id: str = "evening-reflection",
        sa_email: str,
        audit_log,
        memory_bank,
        agent_identity_uuid: str | None = None,
        composer: EveningReflectionComposer,
        completed_tasks_reader: CompletedTasksTodayReader,
        triaged_today_reader: TriagedItemsTodayReader,
        morning_brief_reader: MorningBriefForTodayReader,
        active_risk_flags_reader: ActiveRiskFlagsTodayReader,
        calendar_client: CalendarClient,
        gmail_drafts_client: GmailDraftsClient,
        writer: EveningReflectionWriter,
        prompt_version: str = "v1",
        timezone: str = "America/Los_Angeles",
        voice_memos_reader: RecentVoiceMemosReader | None = None,
        in_flight_decisions_reader: InFlightDecisionsReader | None = None,
        decisions_writer: DecisionsWriter | None = None,
        wins_writer: WinsWriter | None = None,
        triage_publisher: ReflectTriagePublisher | None = None,
        reflection_doc_writer: ReflectionDocWriter | None = None,
        standard_questions: StandardQuestions | None = None,
        chat_client: object | None = None,
        areas_context_reader: AreasContextReader | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
            agent_identity_uuid=agent_identity_uuid,
        )
        self._composer = composer
        self._completed_tasks = completed_tasks_reader
        self._triaged_today = triaged_today_reader
        self._morning_brief = morning_brief_reader
        self._active_risk_flags = active_risk_flags_reader
        self._calendar = calendar_client
        self._gmail = gmail_drafts_client
        self._writer = writer
        self._prompt_version = prompt_version
        self._timezone = timezone
        # ADR 0040 §1 — optional readers; populated by main.py for the
        # mode that needs them. PROMPT mode populates in_flight_decisions;
        # REFLECT mode populates voice_memos. Each reader is unused in
        # the other mode, so leaving it None is the right default for
        # callers (and tests) that only exercise one mode.
        self._voice_memos = voice_memos_reader
        self._in_flight_decisions = in_flight_decisions_reader
        # ADR 0040 §6 — REFLECT-mode extraction targets. Each is optional
        # so PROMPT-mode tests / fallback paths don't need to wire them.
        # When the composer has no structured_llm OR any of these are
        # None, REFLECT mode silently falls back to prose-only output
        # (no PR-A regression).
        self._decisions_writer = decisions_writer
        self._wins_writer = wins_writer
        self._triage_publisher = triage_publisher
        # ADR 0044 — REFLECT-mode artifact is a Google Doc in
        # ``Brain/Areas/Reflections/`` instead of a Gmail draft. Optional
        # so PROMPT-mode tests / the Gmail-only fallback don't need it
        # wired. Production REFLECT tick wires both `reflection_doc_writer`
        # and `chat_client` (the daily Doc + Chat-card link).
        self._reflection_doc_writer = reflection_doc_writer
        self._standard_questions = standard_questions
        self._chat_client = chat_client
        self._areas_context_reader = areas_context_reader
        # Set per-invocation in _run so audit summarization can surface
        # extraction counts. Reset to None at the top of each _run call.
        self._last_dispatch_summary: object | None = None
        self._last_reflection_doc_id: str | None = None
        self._last_reflection_doc_url: str | None = None
        self._last_chat_status: int | None = None

    # ---------------------------------------------------------------- _run

    def _run(self, input: EveningReflectionInput) -> EveningReflectionOutput:
        # Reset per-invocation extraction summary so audit reflects this run only.
        self._last_dispatch_summary = None
        self._last_reflection_doc_id = None
        self._last_reflection_doc_url = None
        self._last_chat_status = None

        # 1) Dedup pre-check (mode-aware per ADR 0040 §7).
        existing = self._writer.find_existing(
            input.recipient_email, input.run_date, mode=input.mode.value
        )
        if existing is not None:
            log.info(
                "evening_reflection.dedup_skip: recipient=%s date=%s existing=%s",
                input.recipient_email,
                input.run_date.isoformat(),
                existing,
            )
            return EveningReflectionOutput(
                reflection_id=existing,
                recipient_email=input.recipient_email,
                local_date=input.run_date,
                body_markdown="(dedup skip — existing reflection retained)",
                sections_used=(),
                prompt_version=self._prompt_version,
                gmail_draft_id=None,
                dedup_skipped=True,
                dedup_existing_reflection_id=existing,
            )

        # 2) Read sources per mode. Each reader's failure is logged but
        # tolerated; the reflection composes from whatever returned.
        # Empty everything → the composer's "unremarkable day" path runs.
        # ADR 0040 §1: PROMPT mode reads in_flight_decisions + triaged
        # (rendered as actionable open followups) + calendar; REFLECT
        # mode reads the v1 five sources + today's voice memos.
        if input.mode is ReflectionMode.PROMPT:
            completed: list = []
            in_flight = _safe_load(
                "in_flight_decisions",
                lambda: self._in_flight_decisions.load() if self._in_flight_decisions else [],
            )
            triaged = _safe_load(
                "triaged_today",
                lambda: self._triaged_today.load(input.recipient_email, input.run_date),
            )
            calendar = _safe_load(
                "calendar",
                lambda: self._calendar.events_for_today(
                    input.recipient_email,
                    input.run_date,
                    tz_name=self._timezone,
                ),
            )
            morning_brief = None
            risk_flags: list = []
            voice_memos: list = []
            blocks = render_section_blocks(
                calendar_events=calendar,
                in_flight_decisions=in_flight,
                open_followups=triaged,
                timezone=self._timezone,
            )
        else:  # REFLECT (default)
            completed = _safe_load(
                "completed_tasks",
                lambda: self._completed_tasks.load(input.recipient_email, input.run_date),
            )
            triaged = _safe_load(
                "triaged_today",
                lambda: self._triaged_today.load(input.recipient_email, input.run_date),
            )
            calendar = _safe_load(
                "calendar",
                lambda: self._calendar.events_for_today(
                    input.recipient_email,
                    input.run_date,
                    tz_name=self._timezone,
                ),
            )
            morning_brief = _safe_load_one(
                "morning_brief",
                lambda: self._morning_brief.load(input.recipient_email, input.run_date),
            )
            risk_flags = _safe_load(
                "active_risk_flags",
                lambda: self._active_risk_flags.load(input.recipient_email, input.run_date),
            )
            voice_memos = _safe_load(
                "voice_memos",
                lambda: self._voice_memos.load() if self._voice_memos else [],
            )
            in_flight = []
            blocks = render_section_blocks(
                completed_tasks=completed,
                triaged_today=triaged,
                calendar_events=calendar,
                morning_brief=morning_brief,
                active_risk_flags=risk_flags,
                voice_memos=voice_memos,
                timezone=self._timezone,
            )

        # 3) Compose body.
        # ADR 0040 §5: REFLECT mode uses structured composition when
        # the composer has a structured_llm AND the dispatch surfaces
        # are wired. On ParseError or any structured-path exception,
        # fall back to prose-only compose so the daily draft still
        # ships (degraded but present).
        reflection_id = new_reflection_id()
        structured_succeeded = False
        structured_payload = None
        if input.mode is ReflectionMode.REFLECT and _structured_path_available(
            self._composer, self._decisions_writer, self._wins_writer, self._triage_publisher
        ):
            try:
                # Use compose_doc when the doc-mode template + writer are
                # wired (ADR 0044). Falls back to compose_structured for
                # backward-compat when only the v2 prose path is present.
                use_doc_path = self._reflection_doc_writer is not None
                if use_doc_path:
                    payload = self._composer.compose_doc(
                        recipient_email=input.recipient_email,
                        recipient_name=_recipient_name(input.recipient_email),
                        run_date=input.run_date,
                        completed_tasks_block=blocks["completed_tasks_block"],
                        triaged_today_block=blocks["triaged_today_block"],
                        calendar_block=blocks["calendar_block"],
                        morning_brief_block=blocks["morning_brief_block"],
                        active_risk_flags_block=blocks["active_risk_flags_block"],
                        voice_memos_block=blocks["voice_memos_block"],
                    )
                else:
                    payload = self._composer.compose_structured(
                        recipient_email=input.recipient_email,
                        recipient_name=_recipient_name(input.recipient_email),
                        run_date=input.run_date,
                        completed_tasks_block=blocks["completed_tasks_block"],
                        triaged_today_block=blocks["triaged_today_block"],
                        calendar_block=blocks["calendar_block"],
                        morning_brief_block=blocks["morning_brief_block"],
                        active_risk_flags_block=blocks["active_risk_flags_block"],
                        voice_memos_block=blocks["voice_memos_block"],
                    )
                structured_payload = payload
                # Dispatch decisions/wins/todos BEFORE Doc/Gmail so a
                # write failure doesn't cause us to lose extracted rows
                # on the retry tick (idempotency keys make re-runs safe
                # but ordering this way means structured state lands first).
                summary = reflect_dispatch(
                    payload,
                    reflection_id=reflection_id,
                    agent_run_id=None,
                    decisions_writer=self._decisions_writer,
                    wins_writer=self._wins_writer,
                    triage_publisher=self._triage_publisher,
                )
                self._last_dispatch_summary = summary
                body_markdown = payload.commentary + render_dispatch_summary_block(payload)
                structured_succeeded = True
            except ParseError as exc:
                log.warning(
                    "evening_reflection.structured_parse_failed: falling back to prose. err=%s",
                    exc,
                )
            except Exception:  # — fall back to prose; never lose the draft
                log.exception("evening_reflection.structured_compose_failed: falling back to prose")

        if not structured_succeeded:
            body_markdown = self._composer.compose(
                recipient_email=input.recipient_email,
                recipient_name=_recipient_name(input.recipient_email),
                run_date=input.run_date,
                **blocks,
            )

        sections_used = _sections_used(
            mode=input.mode,
            completed=completed,
            triaged=triaged,
            calendar=calendar,
            morning_brief=morning_brief,
            risk_flags=risk_flags,
            voice_memos=voice_memos,
            in_flight_decisions=in_flight,
        )
        if structured_succeeded:
            sections_used = (*sections_used, "structured_extraction")

        # 4) Dispatch.
        #    PROMPT mode: Gmail draft (forward-looking anchor; ADR 0040
        #    keeps Gmail surface, the daily anchor isn't artifact-worthy).
        #    REFLECT mode + reflection_doc_writer wired: Google Doc in
        #    ``Brain/Areas/Reflections/`` + Chat-card link (ADR 0044).
        #    REFLECT mode without reflection_doc_writer: Gmail draft
        #    fallback (transitional / test path).
        recipient_name = _recipient_name(input.recipient_email)
        draft_id: str | None = None
        doc_id: str | None = None
        doc_url: str | None = None
        if (
            input.mode is ReflectionMode.REFLECT
            and self._reflection_doc_writer is not None
            and structured_succeeded
            and structured_payload is not None
        ):
            # ADR 0038 §5 — pull top-K Areas/Resources notes related to
            # today's themes via VECTOR_SEARCH. Empty corpus / disabled
            # reader / SDK error → empty tuple, the section elides cleanly.
            areas_context: tuple[AreaNoteSnippet, ...] = ()
            if self._areas_context_reader is not None:
                try:
                    seeds = build_theme_seeds(
                        triaged_today=triaged,
                        voice_memos=voice_memos,
                        active_risk_flags=risk_flags,
                        in_flight_decisions=in_flight,
                    )
                    areas_context = tuple(self._areas_context_reader.load(seeds))
                except Exception:
                    log.exception("evening_reflection.areas_context_failed")
                    areas_context = ()
            body_html = render_reflection_doc_body_html(
                recipient_name=recipient_name,
                run_date=input.run_date,
                payload=structured_payload,
                standard_questions=self._standard_questions,
                completed_tasks_block=blocks["completed_tasks_block"],
                triaged_today_block=blocks["triaged_today_block"],
                calendar_block=blocks["calendar_block"],
                morning_brief_block=blocks["morning_brief_block"],
                active_risk_flags_block=blocks["active_risk_flags_block"],
                voice_memos_block=blocks["voice_memos_block"],
                areas_context=areas_context,
            )
            if areas_context:
                sections_used = (*sections_used, "areas_context")
            doc = self._reflection_doc_writer.write(
                run_date=input.run_date,
                body_html=body_html,
            )
            doc_id = doc.doc_id
            doc_url = doc.doc_url
            self._last_reflection_doc_id = doc_id
            self._last_reflection_doc_url = doc_url
            if self._chat_client is not None:
                chat_result = post_reflection_card(
                    chat=self._chat_client,
                    recipient_name=recipient_name,
                    run_date=input.run_date,
                    doc_url=doc_url,
                )
                self._last_chat_status = chat_result.status if chat_result is not None else None
        else:
            subject_prefix = (
                "Evening Anchor" if input.mode is ReflectionMode.PROMPT else "Evening Reflection"
            )
            subject = f"{subject_prefix} — {input.run_date.strftime('%a, %b %d, %Y')}"
            draft_id = self._gmail.draft(
                recipient_email=input.recipient_email,
                subject=subject,
                body_markdown=body_markdown,
            )

        # 5) Return — main.py invokes writer.write() afterward so the BQ
        # row carries the latency_ms / success bookkeeping. ``reflection_id``
        # was minted at the top of step 3 so dispatch could thread it
        # into ``source_reflection_id`` on extracted rows.
        return EveningReflectionOutput(
            reflection_id=reflection_id,
            recipient_email=input.recipient_email,
            local_date=input.run_date,
            body_markdown=body_markdown,
            sections_used=sections_used,
            prompt_version=self._prompt_version,
            gmail_draft_id=draft_id,
            reflection_doc_id=doc_id,
            reflection_doc_url=doc_url,
            confidence=1.0,
        )

    # ---------------------------------------------- audit summarization

    def _summarize_input(self, input: EveningReflectionInput) -> str | None:
        import json

        return json.dumps(
            {
                "recipient_email": input.recipient_email,
                "run_date": input.run_date.isoformat(),
            }
        )

    def _summarize_output(self, output: EveningReflectionOutput) -> str | None:
        import json

        summary: dict[str, object] = {
            "reflection_id": output.reflection_id,
            "recipient_email": output.recipient_email,
            "local_date": output.local_date.isoformat(),
            "sections_used": list(output.sections_used),
            "body_chars": len(output.body_markdown),
            "gmail_draft_id": output.gmail_draft_id,
            "reflection_doc_id": output.reflection_doc_id,
            "reflection_doc_url": output.reflection_doc_url,
            "chat_card_status": self._last_chat_status,
        }
        if output.dedup_skipped:
            summary["dedup_skipped"] = True
            summary["dedup_existing_reflection_id"] = output.dedup_existing_reflection_id
        # ADR 0040 §6 — surface extraction counts in audit so retros and
        # daily-spend dashboards can see when extraction is firing.
        ds = self._last_dispatch_summary
        if ds is not None:
            summary["extracted_decisions_written"] = getattr(ds, "decisions_written", 0)
            summary["extracted_decisions_skipped"] = getattr(ds, "decisions_skipped", 0)
            summary["extracted_wins_written"] = getattr(ds, "wins_written", 0)
            summary["extracted_wins_skipped"] = getattr(ds, "wins_skipped", 0)
            summary["extracted_todos_published"] = getattr(ds, "todos_published", 0)
            summary["extracted_todos_failed"] = getattr(ds, "todos_failed", 0)
        return json.dumps(summary)


# --------------------------------------------------------- helpers


def _safe_load(name: str, fn):
    try:
        return list(fn())
    except Exception:
        log.exception("evening_reflection.reader_failed name=%s", name)
        return []


def _safe_load_one(name: str, fn):
    try:
        return fn()
    except Exception:
        log.exception("evening_reflection.reader_failed name=%s", name)
        return None


def _sections_used(
    *,
    mode: ReflectionMode,
    completed,
    triaged,
    calendar,
    morning_brief,
    risk_flags,
    voice_memos,
    in_flight_decisions,
) -> tuple[str, ...]:
    sections: list[str] = []
    if completed:
        sections.append("completed_tasks")
    if triaged:
        # In PROMPT mode the triaged reader is rendered as open_followups;
        # surface it under that label so audit summaries reflect what
        # actually went into the prompt.
        sections.append("open_followups" if mode is ReflectionMode.PROMPT else "triaged_today")
    if calendar:
        sections.append("calendar")
    if morning_brief is not None:
        sections.append("morning_brief")
    if risk_flags:
        sections.append("active_risk_flags")
    if voice_memos:
        sections.append("voice_memos")
    if in_flight_decisions:
        sections.append("in_flight_decisions")
    return tuple(sections)


def _recipient_name(email: str) -> str:
    """Best-effort 'first name' from an email address. v1 = single recipient."""
    local_part = email.split("@", 1)[0]
    return local_part.replace(".", " ").replace("_", " ").title()


def _structured_path_available(
    composer: EveningReflectionComposer,
    decisions_writer: DecisionsWriter | None,
    wins_writer: WinsWriter | None,
    triage_publisher: ReflectTriagePublisher | None,
) -> bool:
    """REFLECT mode invokes structured composition only when every dependency
    is wired (composer's structured_llm + all three dispatch surfaces).

    Missing any of them → fall back to prose-only behavior so PR-A
    callers keep working unchanged. Production REFLECT-mode tick wires
    all four; PROMPT-mode tick wires none (and never reaches this
    function because input.mode gates it).
    """
    return (
        getattr(composer, "_structured_llm", None) is not None
        and decisions_writer is not None
        and wins_writer is not None
        and triage_publisher is not None
    )
