"""Brag Spotter Agent — Sunday weekly win-aggregator (ADR 0043).

Subclasses BaseAgent so audit emission and the human-review confidence
threshold are inherited. The `_run` method orchestrates:

  1. Existing-wins-this-week pre-check (composer prompt context).
  2. Read all 5 sources (7-day window each). Per-source failures
     degrade gracefully; the section is just elided.
  3. Compose via Vertex `response_schema` → `BragSpotterPayload`. On
     `ParseError` or empty source data, fall back to a "Quiet week"
     digest so the Sunday ritual still ships (ADR 0043 §7).
  4. Per-candidate dedup pre-check + INSERT into `agent_outputs.wins`.
  5. Dispatch Chat card + Gmail draft (per-channel try/except).
  6. Return `BragSpotterOutput`. The Cloud Run Job entrypoint does NOT
     write a separate dispatch row — Brag Spotter writes wins rows
     during step 4, and the audit row in `agent_audit_log.events`
     (emitted by BaseAgent) carries the dispatch summary in its
     `output` field.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from ..base import BaseAgent
from .composer import BragSpotterComposer, ParseError
from .digest import DigestDispatcher
from .models import (
    BragSpotterInput,
    BragSpotterOutput,
    BragSpotterPayload,
)
from .readers import (
    ExistingWinsForWeekReader,
    RecentDecisionsReader,
    RecentNotesReader,
    RecentReflectionsReader,
    RecentRoutedEventsReader,
    RecentTriagedItemsReader,
)
from .writer import WinsWriter

log = logging.getLogger("agency_brain.agents.brag_spotter.agent")


class BragSpotterAgent(BaseAgent[BragSpotterInput, BragSpotterOutput]):
    def __init__(
        self,
        *,
        agent_id: str = "brag-spotter",
        sa_email: str,
        audit_log,
        memory_bank,
        agent_identity_uuid: str | None = None,
        composer: BragSpotterComposer,
        triaged_items_reader: RecentTriagedItemsReader,
        routed_events_reader: RecentRoutedEventsReader,
        notes_reader: RecentNotesReader,
        decisions_reader: RecentDecisionsReader,
        reflections_reader: RecentReflectionsReader,
        existing_wins_reader: ExistingWinsForWeekReader,
        wins_writer: WinsWriter,
        dispatcher: DigestDispatcher,
        prompt_version: str = "v1",
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
        self._routed_events = routed_events_reader
        self._notes = notes_reader
        self._decisions = decisions_reader
        self._reflections = reflections_reader
        self._existing_wins = existing_wins_reader
        self._writer = wins_writer
        self._dispatcher = dispatcher
        self._prompt_version = prompt_version

    # ---------------------------------------------------------------- _run

    def _run(self, input: BragSpotterInput) -> BragSpotterOutput:
        run_id = str(uuid.uuid4())
        digest_id = run_id  # one-digest-per-run; reused as audit anchor

        # 1) Existing wins for the week — feeds the prompt + dedup signal.
        existing_wins = _safe_load(
            "existing_wins",
            lambda: self._existing_wins.load(input.week_of),
        )

        # 2) Read 5 sources.
        triaged = _safe_load("triaged_items", self._triaged_items.load)
        routed = _safe_load("routed_events", self._routed_events.load)
        notes = _safe_load("notes", self._notes.load)
        decisions = _safe_load("decisions", self._decisions.load)
        reflections = _safe_load("reflections", self._reflections.load)

        sources_seen = _which_sources_returned(triaged, routed, notes, decisions, reflections)

        # 3) Compose. If the LLM returns garbage, fall back to a
        # quiet-week digest so we still ship the Sunday email + Chat.
        try:
            payload = self._composer.compose(
                recipient_email=input.recipient_email,
                week_of=input.week_of,
                triaged_items=triaged,
                routed_events=routed,
                notes=notes,
                decisions=decisions,
                reflections=reflections,
                existing_wins=existing_wins,
            )
        except ParseError as exc:
            log.warning(
                "brag_spotter.compose.parse_error: falling back to quiet-week digest err=%s",
                exc,
            )
            payload = _quiet_week_payload(input.week_of)

        # 4) Write candidates to wins table (per-candidate dedup).
        wins_written = 0
        wins_skipped = 0
        now = datetime.now(UTC)
        for candidate in payload.candidates:
            outcome = self._writer.write(
                candidate,
                week_of=input.week_of,
                agent_run_id=run_id,
                now=now,
            )
            if outcome.written:
                wins_written += 1
            else:
                wins_skipped += 1

        # 5) Dispatch Chat + Gmail (per-channel try/except inside).
        dispatch = self._dispatcher.dispatch(
            recipient_email=input.recipient_email,
            week_of=input.week_of,
            commentary=payload.commentary,
            new_candidates=list(payload.candidates),
            existing_wins=list(existing_wins),
        )

        return BragSpotterOutput(
            digest_id=digest_id,
            recipient_email=input.recipient_email,
            week_of=input.week_of,
            body_markdown=payload.commentary,
            candidates=payload.candidates,
            wins_written=wins_written,
            wins_skipped=wins_skipped,
            sources_seen=sources_seen,
            chat_status=dispatch.chat_status,
            gmail_draft_id=dispatch.gmail_draft_id,
            confidence=1.0,
        )

    # ---------------------------------------------- audit summarization

    def _summarize_input(self, input: BragSpotterInput) -> str | None:
        import json

        return json.dumps(
            {
                "recipient_email": input.recipient_email,
                "run_date": input.run_date.isoformat(),
                "week_of": input.week_of.isoformat(),
            }
        )

    def _summarize_output(self, output: BragSpotterOutput) -> str | None:
        import json

        return json.dumps(
            {
                "digest_id": output.digest_id,
                "recipient_email": output.recipient_email,
                "week_of": output.week_of.isoformat(),
                "candidates_count": len(output.candidates),
                "wins_written": output.wins_written,
                "wins_skipped": output.wins_skipped,
                "sources_seen": list(output.sources_seen),
                "chat_status": output.chat_status,
                "gmail_draft_id": output.gmail_draft_id,
                "body_chars": len(output.body_markdown),
            }
        )


# ---------------------------------------------------------------- helpers


def _safe_load(name: str, fn):
    try:
        return list(fn())
    except Exception:
        log.exception("brag_spotter.reader_failed name=%s", name)
        return []


def _which_sources_returned(
    triaged: list,
    routed: list,
    notes: list,
    decisions: list,
    reflections: list,
) -> tuple[str, ...]:
    seen: list[str] = []
    if triaged:
        seen.append("triaged_items")
    if routed:
        seen.append("routed_events")
    if notes:
        seen.append("notes")
    if decisions:
        seen.append("decisions")
    if reflections:
        seen.append("reflections")
    return tuple(seen)


def _quiet_week_payload(week_of) -> BragSpotterPayload:
    """Fallback used when the LLM compose fails (ADR 0043 §7)."""
    week_human = week_of.strftime("%B %d, %Y")
    commentary = (
        f"Week of {week_human}: nothing structured to flag this week. The "
        "Sunday ritual still ran — review past reflections + notes for "
        "wins that didn't surface loudly enough to be auto-detected."
    )
    return BragSpotterPayload(
        commentary=commentary,
        candidates=(),
    )
