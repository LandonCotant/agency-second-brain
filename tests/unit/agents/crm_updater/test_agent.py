"""Tests for ``CrmUpdaterAgent`` — orchestration + audit emission."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.crm_updater.agent import CrmUpdaterAgent
from agency_brain.agents.crm_updater.extractor import ExtractorResult
from agency_brain.agents.crm_updater.hipaa_filter import HipaaFilter
from agency_brain.agents.crm_updater.models import (
    CrmUpdaterInput,
    DraftWriteResult,
    ExtractedTask,
    ExtractionResult,
    GmailMessage,
)
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank


class _FakeExtractor:
    def __init__(
        self,
        *,
        extraction: ExtractionResult,
        cost: float = 0.005,
        confidence: float = 0.85,
    ) -> None:
        self._extraction = extraction
        self._cost = cost
        self._confidence = confidence
        self.last_message: GmailMessage | None = None

    def extract(
        self,
        *,
        message: GmailMessage,
        known_account_names: tuple[str, ...] = (),
        known_contact_emails: tuple[str, ...] = (),
    ) -> ExtractorResult:
        self.last_message = message
        return ExtractorResult(
            extraction=self._extraction,
            cost_usd=self._cost,
            confidence=self._confidence,
        )


class _FakeWriter:
    def __init__(self, *, result: DraftWriteResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def write(
        self,
        *,
        tasks,
        contact_updates,
        account_mentions,
        run_timestamp_iso: str,
    ) -> DraftWriteResult:
        self.calls.append(
            {
                "tasks": tasks,
                "contact_updates": contact_updates,
                "account_mentions": account_mentions,
                "run_timestamp_iso": run_timestamp_iso,
            }
        )
        return self.result


class _FakeBQ:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict]]] = []

    def insert_rows_json(self, table_ref, rows):
        self.inserted.append((table_ref, list(rows)))
        return []


def _msg(*, frm="sarah@example.com", to=("owner@example.com",), cc=()) -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        subject="Re: Q2",
        from_addr=frm,
        to_addrs=to,
        cc_addrs=cc,
        body_text="Body.",
        received_at=datetime.now(UTC),
    )


def _full_extraction() -> ExtractionResult:
    return ExtractionResult(
        extracted_tasks=(
            ExtractedTask(
                title="Reply to Sarah",
                due_date=None,
                linked_account_name=None,
                linked_contact_email="sarah@example.com",
                confidence=0.9,
            ),
        ),
        contact_updates=(),
        account_mentions=(),
    )


def _make_agent(
    *,
    extraction: ExtractionResult,
    write_result: DraftWriteResult,
    hipaa_domains=(),
) -> tuple[CrmUpdaterAgent, _FakeBQ, _FakeExtractor, _FakeWriter]:
    bq = _FakeBQ()
    audit = AuditLogClient(project_id="p", bq_client=bq)
    extractor = _FakeExtractor(extraction=extraction)
    writer = _FakeWriter(result=write_result)

    return (
        CrmUpdaterAgent(
            extractor=extractor,
            writer_factory=lambda message_id: writer,
            hipaa_filter=HipaaFilter(hipaa_domains=hipaa_domains),
            sa_email="asb-crm-updater-sa@p.iam.gserviceaccount.com",
            audit_log=audit,
            memory_bank=InMemoryMemoryBank(),
            known_account_names=("Acme",),
            known_contact_emails=("sarah@example.com",),
        ),
        bq,
        extractor,
        writer,
    )


def test_invoke_extracts_writes_and_emits_audit() -> None:
    agent, bq, extractor, writer = _make_agent(
        extraction=_full_extraction(),
        write_result=DraftWriteResult(
            task_record_ids=("recTASK",),
            contact_updates_appended=0,
            account_updates_appended=0,
        ),
    )
    output = agent.invoke(CrmUpdaterInput(message=_msg()))
    assert output.skipped is False
    assert output.write_result.task_record_ids == ("recTASK",)
    assert extractor.last_message is not None
    assert len(writer.calls) == 1
    # Audit emitted.
    assert len(bq.inserted) == 1
    row = bq.inserted[0][1][0]
    assert row["agent_id"] == "crm-updater"
    assert row["success"] is True


def test_invoke_skips_hipaa_blocked_email() -> None:
    agent, bq, extractor, writer = _make_agent(
        extraction=_full_extraction(),
        write_result=DraftWriteResult(
            task_record_ids=(), contact_updates_appended=0, account_updates_appended=0
        ),
        hipaa_domains=("hospitalcorp.com",),
    )
    output = agent.invoke(CrmUpdaterInput(message=_msg(frm="someone@hospitalcorp.com")))
    assert output.skipped is True
    assert "HIPAA-flagged" in (output.skip_reason or "")
    # Extractor and writer are NOT called.
    assert extractor.last_message is None
    assert writer.calls == []
    # Audit row still emitted (BaseAgent contract).
    assert len(bq.inserted) == 1


def test_invoke_audit_carries_summary_fields() -> None:
    agent, bq, _, _ = _make_agent(
        extraction=_full_extraction(),
        write_result=DraftWriteResult(
            task_record_ids=("recTASK",),
            contact_updates_appended=2,
            account_updates_appended=1,
        ),
    )
    agent.invoke(CrmUpdaterInput(message=_msg()))
    row = bq.inserted[0][1][0]
    summary = row["output"] or ""
    assert "tasks=1" in summary
    assert "contact_appends=2" in summary
    assert "account_appends=1" in summary


# ---------------------------------------------------------------------------
# ADR 0049 — Gmail-into-corpus side-effect integration
# ---------------------------------------------------------------------------


class _FakeNotesWriter:
    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.calls: list[GmailMessage] = []

    def write(self, message: GmailMessage):
        from agency_brain.agents.crm_updater.notes_writer import (
            NotesWriteOutcome,
        )

        self.calls.append(message)
        if self.raises is not None:
            raise self.raises
        return NotesWriteOutcome(
            note_id=f"email:{message.message_id}",
            inserted=True,
        )


def test_notes_writer_invoked_after_successful_extraction() -> None:
    """ADR 0049 — every non-HIPAA email yields a corpus row alongside
    the Airtable drafts."""
    bq = _FakeBQ()
    audit = AuditLogClient(project_id="p", bq_client=bq)
    extractor = _FakeExtractor(extraction=_full_extraction())
    writer = _FakeWriter(
        result=DraftWriteResult(
            task_record_ids=("recTASK",),
            contact_updates_appended=0,
            account_updates_appended=0,
        )
    )
    notes_writer = _FakeNotesWriter()
    agent = CrmUpdaterAgent(
        extractor=extractor,
        writer_factory=lambda message_id: writer,
        hipaa_filter=HipaaFilter(hipaa_domains=()),
        sa_email="asb-crm-updater-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        notes_writer=notes_writer,
    )

    msg = _msg()
    output = agent.invoke(CrmUpdaterInput(message=msg))

    assert output.skipped is False
    assert notes_writer.calls == [msg]
    assert output.notes_write is not None
    assert output.notes_write.inserted is True


def test_notes_writer_skipped_on_hipaa_block() -> None:
    """HIPAA-blocked emails skip BOTH the extractor and the notes write —
    by design (defense in depth: HIPAA content never reaches /ask)."""
    notes_writer = _FakeNotesWriter()
    bq = _FakeBQ()
    audit = AuditLogClient(project_id="p", bq_client=bq)
    agent = CrmUpdaterAgent(
        extractor=_FakeExtractor(extraction=_full_extraction()),
        writer_factory=lambda message_id: _FakeWriter(
            result=DraftWriteResult(
                task_record_ids=(),
                contact_updates_appended=0,
                account_updates_appended=0,
            )
        ),
        hipaa_filter=HipaaFilter(hipaa_domains=("hospitalcorp.com",)),
        sa_email="asb-crm-updater-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        notes_writer=notes_writer,
    )

    output = agent.invoke(CrmUpdaterInput(message=_msg(frm="someone@hospitalcorp.com")))

    assert output.skipped is True
    assert notes_writer.calls == []
    assert output.notes_write is None


def test_notes_writer_failure_does_not_break_draft_pipeline() -> None:
    """A failed corpus write must NOT propagate — Airtable drafts are
    the primary product of this Job."""
    notes_writer = _FakeNotesWriter(raises=RuntimeError("bq 5xx"))
    bq = _FakeBQ()
    audit = AuditLogClient(project_id="p", bq_client=bq)
    writer = _FakeWriter(
        result=DraftWriteResult(
            task_record_ids=("recTASK",),
            contact_updates_appended=0,
            account_updates_appended=0,
        )
    )
    agent = CrmUpdaterAgent(
        extractor=_FakeExtractor(extraction=_full_extraction()),
        writer_factory=lambda message_id: writer,
        hipaa_filter=HipaaFilter(hipaa_domains=()),
        sa_email="asb-crm-updater-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        notes_writer=notes_writer,
    )

    output = agent.invoke(CrmUpdaterInput(message=_msg()))

    assert output.skipped is False
    assert output.write_result.task_record_ids == ("recTASK",)
    assert output.notes_write is None  # writer raised → outcome stays None
