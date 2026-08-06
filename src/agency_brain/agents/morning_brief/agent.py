"""WS-G3 Morning Brief Agent. PRD §5.6 + spec §5 + ADR 0029.

Subclasses BaseAgent so audit emission, HIPAA pre-flight, and the
human-review confidence threshold are inherited. The `_run` method
orchestrates the four readers + calendar fetch + composer + Gmail
drafts + writer dedup chain.

Order of operations in `_run`:
  1. Per-recipient-per-day dedup pre-check (writer.find_existing).
     Hit → return MorningBriefOutput with dedup_skipped=True; no LLM,
     no Gmail draft.
  2. Read all five context sources. Failures degrade gracefully —
     a missing Calendar response or empty Risk Watcher doesn't break
     the brief; the section is just elided.
  3. Render prompt + call LLM via composer.compose.
  4. Draft Gmail via gmail_drafts_client.draft. The draft id is the
     "load-bearing receipt" — without it, the daily ritual didn't
     happen.
  5. Return MorningBriefOutput. The Cloud Run Job entrypoint
     (`main.py`) calls writer.write() AFTER `invoke()` returns, so
     the audit row + the BQ row both reflect the dispatch outcome.
"""

from __future__ import annotations

import logging

from ..base import BaseAgent
from .calendar_client import CalendarClient
from .composer import MorningBriefComposer, render_section_blocks
from .gmail_drafts_client import GmailDraftsClient
from .models import MorningBriefInput, MorningBriefOutput
from .readers import (
    DraftsAwaitingReviewReader,
    OpenTasksForOwnerReader,
    RiskFlagsReader,
    TriagedItemsForOwnerReader,
)
from .writer import MorningBriefWriter, new_brief_id

log = logging.getLogger("agency_brain.agents.morning_brief.agent")


class MorningBriefAgent(BaseAgent[MorningBriefInput, MorningBriefOutput]):
    def __init__(
        self,
        *,
        agent_id: str = "morning-brief",
        sa_email: str,
        audit_log,
        memory_bank,
        agent_identity_uuid: str | None = None,
        composer: MorningBriefComposer,
        triaged_items_reader: TriagedItemsForOwnerReader,
        open_tasks_reader: OpenTasksForOwnerReader,
        risk_flags_reader: RiskFlagsReader,
        drafts_awaiting_reader: DraftsAwaitingReviewReader,
        calendar_client: CalendarClient,
        gmail_drafts_client: GmailDraftsClient,
        writer: MorningBriefWriter,
        prompt_version: str = "v1",
        timezone: str = "America/Los_Angeles",
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
            agent_identity_uuid=agent_identity_uuid,
        )
        self._composer = composer
        self._triaged_items = triaged_items_reader
        self._open_tasks = open_tasks_reader
        self._risk_flags = risk_flags_reader
        self._drafts_awaiting = drafts_awaiting_reader
        self._calendar = calendar_client
        self._gmail = gmail_drafts_client
        self._writer = writer
        self._prompt_version = prompt_version
        self._timezone = timezone

    # ---------------------------------------------------------------- _run

    def _run(self, input: MorningBriefInput) -> MorningBriefOutput:
        # 1) Dedup pre-check.
        existing = self._writer.find_existing(input.recipient_email, input.run_date)
        if existing is not None:
            log.info(
                "morning_brief.dedup_skip: recipient=%s date=%s existing=%s",
                input.recipient_email,
                input.run_date.isoformat(),
                existing,
            )
            return MorningBriefOutput(
                brief_id=existing,  # surface the existing id, not a fresh one
                recipient_email=input.recipient_email,
                local_date=input.run_date,
                body_markdown="(dedup skip — existing brief retained)",
                sections_used=(),
                prompt_version=self._prompt_version,
                gmail_draft_id=None,
                dedup_skipped=True,
                dedup_existing_brief_id=existing,
            )

        # 2) Read sources. Each reader's failure is logged but tolerated;
        # the brief composes from whatever returned. Empty everything → the
        # composer's "Quiet day" path runs.
        triaged = _safe_load(
            "triaged_items",
            lambda: self._triaged_items.load(input.recipient_email),
        )
        tasks = _safe_load(
            "open_tasks",
            lambda: self._open_tasks.load(input.recipient_email),
        )
        flags = _safe_load(
            "risk_flags",
            lambda: self._risk_flags.load(input.recipient_email),
        )
        drafts = _safe_load(
            "drafts_awaiting",
            lambda: self._drafts_awaiting.load(input.recipient_email),
        )
        calendar = _safe_load(
            "calendar",
            lambda: self._calendar.events_for_today(
                input.recipient_email,
                input.run_date,
                tz_name=self._timezone,
            ),
        )

        # 3) Render section blocks + compose body.
        blocks = render_section_blocks(
            triaged_items=triaged,
            open_tasks=tasks,
            risk_flags=flags,
            drafts_awaiting=drafts,
            calendar_events=calendar,
            timezone=self._timezone,
        )
        body_markdown = self._composer.compose(
            recipient_email=input.recipient_email,
            recipient_name=_recipient_name(input.recipient_email),
            run_date=input.run_date,
            **blocks,
        )

        sections_used = _sections_used(triaged, tasks, flags, drafts, calendar)

        # 4) Draft Gmail.
        subject = f"Morning Brief — {input.run_date.strftime('%a, %b %d, %Y')}"
        draft_id = self._gmail.draft(
            recipient_email=input.recipient_email,
            subject=subject,
            body_markdown=body_markdown,
        )

        # 5) Return — the Cloud Run Job entrypoint invokes writer.write()
        # afterward so the BQ row has the latency_ms / success bookkeeping.
        return MorningBriefOutput(
            brief_id=new_brief_id(),
            recipient_email=input.recipient_email,
            local_date=input.run_date,
            body_markdown=body_markdown,
            sections_used=sections_used,
            prompt_version=self._prompt_version,
            gmail_draft_id=draft_id,
            confidence=1.0,
        )

    # ---------------------------------------------- audit summarization

    def _summarize_input(self, input: MorningBriefInput) -> str | None:
        import json

        return json.dumps(
            {
                "recipient_email": input.recipient_email,
                "run_date": input.run_date.isoformat(),
            }
        )

    def _summarize_output(self, output: MorningBriefOutput) -> str | None:
        import json

        summary: dict[str, object] = {
            "brief_id": output.brief_id,
            "recipient_email": output.recipient_email,
            "local_date": output.local_date.isoformat(),
            "sections_used": list(output.sections_used),
            "body_chars": len(output.body_markdown),
            "gmail_draft_id": output.gmail_draft_id,
        }
        if output.dedup_skipped:
            summary["dedup_skipped"] = True
            summary["dedup_existing_brief_id"] = output.dedup_existing_brief_id
        return json.dumps(summary)


# --------------------------------------------------------- helpers


def _safe_load(name: str, fn):
    try:
        return list(fn())
    except Exception:
        log.exception("morning_brief.reader_failed name=%s", name)
        return []


def _sections_used(triaged, tasks, flags, drafts, calendar) -> tuple[str, ...]:
    sections: list[str] = []
    if triaged:
        sections.append("top_three")
    if drafts:
        sections.append("drafts_awaiting_review")
    if flags:
        sections.append("risk_flags")
    if calendar:
        sections.append("calendar")
    if len(triaged) > 3:
        sections.append("rest_of_queue")
    return tuple(sections)


def _recipient_name(email: str) -> str:
    """Best-effort 'first name' from an email address. v1 = single recipient."""
    local_part = email.split("@", 1)[0]
    return local_part.replace(".", " ").replace("_", " ").title()
