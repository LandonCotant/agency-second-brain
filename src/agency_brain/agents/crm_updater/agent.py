"""CRM Auto-updater orchestrator (ADR 0047).

Pipeline per email:
  1. (BaseAgent) HIPAA pre-flight on input.aspects — usually empty.
  2. HipaaFilter check on the email's participants → skip + audit if blocked.
  3. Extract via Vertex Flash with structured output.
  4. Write drafts: Tasks (POST), Contact Pending Updates (PATCH),
     Account Pending Updates (PATCH).
  5. Apply ``secondbrain-processed`` label (caller responsibility — kept
     out of the orchestrator so retry semantics live at the loop layer).

Caller (``main.py``) iterates over messages, calls ``invoke()`` for
each, then applies the dedup label only on successful invocations.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ...common.audit_log import AuditLogClient
from ...common.memory_bank import MemoryBank
from ..base import BaseAgent
from .extractor import Extractor
from .hipaa_filter import HipaaFilter
from .models import (
    CrmUpdaterInput,
    CrmUpdaterOutput,
    DraftWriteResult,
    ExtractionResult,
)
from .notes_writer import CrmNotesWriter, NotesWriteOutcome

log = logging.getLogger("agency_brain.agents.crm_updater.agent")


class CrmUpdaterAgent(BaseAgent[CrmUpdaterInput, CrmUpdaterOutput]):
    def __init__(
        self,
        *,
        extractor: Extractor,
        writer_factory,  # callable: (message_id: str) -> CrmDraftWriter
        hipaa_filter: HipaaFilter,
        sa_email: str,
        audit_log: AuditLogClient,
        memory_bank: MemoryBank,
        known_account_names: tuple[str, ...] = (),
        known_contact_emails: tuple[str, ...] = (),
        agent_id: str = "crm-updater",
        notes_writer: CrmNotesWriter | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
        )
        self._extractor = extractor
        self._writer_factory = writer_factory
        self._hipaa_filter = hipaa_filter
        self._known_account_names = known_account_names
        self._known_contact_emails = known_contact_emails
        self._notes_writer = notes_writer

    def _run(self, input: CrmUpdaterInput) -> CrmUpdaterOutput:
        message = input.message

        # HIPAA pre-flight at the application layer (PRD §4.1 layer 3).
        hipaa_check = self._hipaa_filter.check(message)
        if not hipaa_check.allowed:
            log.warning(
                "crm_updater.agent.hipaa_blocked message_id=%s offenders=%s",
                message.message_id,
                hipaa_check.blocking_addresses,
            )
            offenders = ", ".join(hipaa_check.blocking_addresses)
            return CrmUpdaterOutput(
                extraction=_empty_extraction(),
                write_result=_empty_write_result(),
                skipped=True,
                skip_reason=f"HIPAA-flagged participants: {offenders}",
                confidence=0.0,
                cost_usd=0.0,
            )

        extracted = self._extractor.extract(
            message=message,
            known_account_names=self._known_account_names,
            known_contact_emails=self._known_contact_emails,
        )

        run_ts = datetime.now(UTC).isoformat()
        writer = self._writer_factory(message.message_id)
        write_result = writer.write(
            tasks=extracted.extraction.extracted_tasks,
            contact_updates=extracted.extraction.contact_updates,
            account_mentions=extracted.extraction.account_mentions,
            run_timestamp_iso=run_ts,
        )

        # ADR 0049 — Gmail-into-corpus side effect. Drives /ask retrieval
        # on email bodies. Isolated from the draft-write path: a failed
        # corpus write logs but does not fail the agent (the Airtable
        # drafts are the primary product). Disabled when no writer is
        # injected (test/back-compat callers).
        notes_outcome: NotesWriteOutcome | None = None
        if self._notes_writer is not None:
            try:
                notes_outcome = self._notes_writer.write(message)
            except Exception:
                log.exception(
                    "crm_updater.agent.notes_write_unhandled message_id=%s",
                    message.message_id,
                )

        return CrmUpdaterOutput(
            extraction=extracted.extraction,
            write_result=write_result,
            skipped=False,
            skip_reason=None,
            confidence=extracted.confidence,
            cost_usd=extracted.cost_usd,
            notes_write=notes_outcome,
        )

    # ----------------------------------------------- audit summarizers

    def _summarize_input(self, input: CrmUpdaterInput) -> str | None:
        msg = input.message
        return (
            f"msg_id={msg.message_id} from={msg.from_addr} "
            f"to_count={len(msg.to_addrs)} subj_chars={len(msg.subject)} "
            f"body_chars={len(msg.body_text)}"
        )

    def _summarize_output(self, output: CrmUpdaterOutput) -> str | None:
        if output.skipped:
            return f"skipped reason={output.skip_reason}"
        wr = output.write_result
        return (
            f"tasks={len(output.extraction.extracted_tasks)} "
            f"contact_updates={len(output.extraction.contact_updates)} "
            f"account_mentions={len(output.extraction.account_mentions)} "
            f"task_ids={len(wr.task_record_ids)} "
            f"contact_appends={wr.contact_updates_appended} "
            f"account_appends={wr.account_updates_appended} "
            f"cost_usd={output.cost_usd}"
        )


# --------------------------------------------------------------------- helpers


def _empty_extraction() -> ExtractionResult:
    return ExtractionResult(
        extracted_tasks=(),
        contact_updates=(),
        account_mentions=(),
    )


def _empty_write_result() -> DraftWriteResult:
    return DraftWriteResult(
        task_record_ids=(),
        contact_updates_appended=0,
        account_updates_appended=0,
    )
