"""CRM Auto-updater dataclasses (ADR 0047)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GmailMessageRef:
    """Lightweight reference returned by `users.messages.list`."""

    message_id: str
    thread_id: str
    history_id: str | None = None


@dataclass(frozen=True)
class GmailMessage:
    """Full message body fetched via `users.messages.get`.

    ``history_id`` mirrors the same-named field on the API response.
    The CRM Auto-updater's main loop reads it from the most-recently
    fetched message to advance the run checkpoint
    (``RunCheckpoint.history_id_after``). Reserved for future
    Gmail-history-API integration — v1 doesn't depend on it for
    correctness, but the loop reads it unconditionally so it must be
    present on the dataclass.
    """

    message_id: str
    thread_id: str
    subject: str
    from_addr: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    body_text: str
    received_at: datetime
    label_ids: tuple[str, ...] = ()
    history_id: str | None = None


@dataclass(frozen=True)
class ExtractedTask:
    """One task drafted from an email."""

    title: str
    due_date: str | None
    linked_account_name: str | None
    linked_contact_email: str | None
    confidence: float


@dataclass(frozen=True)
class ContactUpdate:
    """One contact-update suggestion."""

    contact_email: str
    last_contact_date: str | None
    next_followup_suggested: str | None
    warmth_change: str  # "unchanged" | "warmer" | "cooler"
    context_note: str


@dataclass(frozen=True)
class AccountMention:
    """One account-mention suggestion."""

    account_name: str
    context_note: str
    new_contacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionResult:
    """Structured output of the extractor for a single email."""

    extracted_tasks: tuple[ExtractedTask, ...]
    contact_updates: tuple[ContactUpdate, ...]
    account_mentions: tuple[AccountMention, ...]


@dataclass(frozen=True)
class DraftWriteResult:
    """Bookkeeping returned by AirtableWriter for a single email's drafts."""

    task_record_ids: tuple[str, ...]
    contact_updates_appended: int
    account_updates_appended: int


@dataclass(frozen=True)
class CrmUpdaterInput:
    """Input to ``CrmUpdaterAgent.invoke`` per email."""

    message: GmailMessage
    aspects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CrmUpdaterOutput:
    """Per-email output.

    ``notes_write`` is the bookkeeping from the ADR 0049 corpus-write
    side effect (``CrmNotesWriter.write``). ``None`` when the writer is
    not injected (test paths) or the email is HIPAA-skipped before the
    notes write would fire.
    """

    extraction: ExtractionResult
    write_result: DraftWriteResult
    skipped: bool
    skip_reason: str | None
    confidence: float
    cost_usd: float
    notes_write: object | None = None
    """Concrete type: ``notes_writer.NotesWriteOutcome | None``. Typed
    as ``object`` to avoid a circular import; runtime callers cast."""


@dataclass(frozen=True)
class RunCheckpoint:
    """A single row of ``agent_outputs.crm_updater_runs``.

    The since-history-id is the resumption cursor; the agent uses
    ``MAX(history_id_after) FROM crm_updater_runs WHERE success = TRUE``
    as the bound for the next ``users.messages.list`` call.
    """

    run_id: str
    started_at: datetime
    ended_at: datetime
    history_id_after: str | None
    messages_processed: int
    drafts_created: int
    errors: int
    success: bool
