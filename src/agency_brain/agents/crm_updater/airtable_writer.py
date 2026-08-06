"""Airtable writer for the CRM Auto-updater (ADR 0047).

Three operations:
  1. ``draft_task`` — POST to /v0/{base}/Tasks with `Approval Status =
     "Drafted by Agent"` (existing pattern from ``triage/writers.py``).
  2. ``draft_contact_update`` — find Contact by email, PATCH the
     ``Pending Updates`` long-text field with a timestamped block.
  3. ``draft_account_update`` — find Account by name, PATCH similarly.

Drafts-only: humans approve every change. No DELETE, no UPDATE on
canonical fields.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .models import (
    AccountMention,
    ContactUpdate,
    DraftWriteResult,
    ExtractedTask,
)

if TYPE_CHECKING:
    import requests

log = logging.getLogger("agency_brain.agents.crm_updater.airtable_writer")

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
TASKS_TABLE = "Tasks"
CONTACTS_TABLE = "Contacts"
ACCOUNTS_TABLE = "Accounts"
PROJECTS_TABLE = "Projects"

# Projects.Status values that mean "this Project is done; don't link new
# Tasks to it." Anything not in this set (including blank) counts as
# linkable. Soft denylist by design — adding a new active status to the
# Operations base does NOT require a code change.
_PROJECT_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"Complete", "Cancelled", "Closed", "Archived", "Done"}
)


class AirtableCRMWriteError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Airtable CRM write HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class AirtableCRMWriteClient:
    """Find-by-key + POST + PATCH against the Operations base."""

    def __init__(
        self,
        *,
        base_id: str,
        pat: str,
        session: Any = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_id = base_id
        self._pat = pat
        self._session = session
        self._timeout = timeout

    def _http(self) -> requests.Session:
        if self._session is None:
            import requests as _requests

            self._session = _requests.Session()
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self._pat}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    # -------------------------------------------------------------- TASKS

    def create_task(self, fields: dict) -> str:
        url = f"{AIRTABLE_API_BASE}/{self._base_id}/" f"{quote(TASKS_TABLE, safe='')}"
        response = self._http().post(
            url,
            json={"fields": fields, "typecast": True},
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise AirtableCRMWriteError(response.status_code, response.text)
        record_id = response.json().get("id")
        if not record_id:
            raise AirtableCRMWriteError(
                response.status_code,
                f"missing 'id' in Tasks POST response: {response.text[:500]}",
            )
        return str(record_id)

    # ----------------------------------------------------------- CONTACTS

    def find_contact_by_email(self, email: str) -> tuple[str, str] | None:
        """Return ``(record_id, current_pending_updates)`` or None."""
        if not email:
            return None
        formula = f"LOWER({{Email}})='{email.lower().replace(chr(39), chr(92) + chr(39))}'"
        return self._find_one(
            table=CONTACTS_TABLE,
            formula=formula,
            fields=("Pending Updates",),
        )

    def append_contact_pending(self, *, record_id: str, current: str, block: str) -> None:
        new_value = _append_block(current=current, block=block)
        self._patch(
            table=CONTACTS_TABLE,
            record_id=record_id,
            fields={"Pending Updates": new_value},
        )

    # ----------------------------------------------------------- PROJECTS

    def find_active_project_for_account_name(self, account_name: str) -> str | None:
        """Return the record id of an active Project linked to the named
        Account, or None if none can be confidently picked.

        Used by ``CrmDraftWriter`` to resolve ``ExtractedTask.linked_account_name``
        to a real Project instead of always defaulting drafted Tasks to
        the Triage Inbox.

        Heuristics:
        - Substring + case-insensitive match against the Project's
          ``Account`` linked field (which renders to the linked Account's
          primary "Company Name" in formula context).
        - Status is NOT in ``_PROJECT_TERMINAL_STATUSES`` (Complete /
          Cancelled / Closed / Archived / Done). Blank Status counts as
          linkable.
        - Returns ONLY when exactly one candidate remains after the
          filter. Ambiguous matches (multiple active Projects on the
          same Account) → None so the writer falls back to inbox, where
          a human can re-route.
        - Any API or formula error → None (fall back is always safe).
        """
        if not account_name or not account_name.strip():
            return None
        escaped = account_name.replace(chr(39), chr(92) + chr(39))
        formula = f"FIND(LOWER('{escaped}'), LOWER({{Account}})) > 0"
        try:
            candidates = self._find_many(
                table=PROJECTS_TABLE,
                formula=formula,
                fields=("Status",),
                max_records=10,
            )
        except AirtableCRMWriteError:
            log.warning(
                "crm_updater.airtable_writer.project_lookup_failed account=%s",
                account_name,
            )
            return None
        active = [
            rec
            for rec in candidates
            if str((rec.get("fields") or {}).get("Status") or "") not in _PROJECT_TERMINAL_STATUSES
        ]
        if len(active) != 1:
            log.info(
                "crm_updater.airtable_writer.project_lookup_ambiguous "
                "account=%s candidates=%d active=%d → fallback to inbox",
                account_name,
                len(candidates),
                len(active),
            )
            return None
        return str(active[0].get("id") or "") or None

    # ----------------------------------------------------------- ACCOUNTS

    def find_account_by_name(self, name: str) -> tuple[str, str] | None:
        if not name:
            return None
        # Airtable formulas: SEARCH() returns position or 0 — used for
        # case-insensitive substring match against the primary "Company Name".
        escaped = name.replace(chr(39), chr(92) + chr(39))
        formula = f"FIND(LOWER('{escaped}'), LOWER({{Company Name}})) > 0"
        return self._find_one(
            table=ACCOUNTS_TABLE,
            formula=formula,
            fields=("Pending Updates",),
        )

    def append_account_pending(self, *, record_id: str, current: str, block: str) -> None:
        new_value = _append_block(current=current, block=block)
        self._patch(
            table=ACCOUNTS_TABLE,
            record_id=record_id,
            fields={"Pending Updates": new_value},
        )

    # ------------------------------------------------------------- helpers

    def _find_one(
        self,
        *,
        table: str,
        formula: str,
        fields: tuple[str, ...],
    ) -> tuple[str, str] | None:
        records = self._find_many(table=table, formula=formula, fields=fields, max_records=1)
        if not records:
            return None
        record = records[0]
        record_id = str(record.get("id") or "")
        current = str((record.get("fields") or {}).get("Pending Updates") or "")
        return record_id, current

    def _find_many(
        self,
        *,
        table: str,
        formula: str,
        fields: tuple[str, ...],
        max_records: int,
    ) -> list[dict]:
        url = f"{AIRTABLE_API_BASE}/{self._base_id}/{quote(table, safe='')}"
        params = {
            "filterByFormula": formula,
            "maxRecords": max_records,
            "fields[]": list(fields),
        }
        response = self._http().get(url, params=params, timeout=self._timeout)
        if response.status_code >= 400:
            raise AirtableCRMWriteError(response.status_code, response.text)
        return list(response.json().get("records") or [])

    def _patch(
        self,
        *,
        table: str,
        record_id: str,
        fields: dict,
    ) -> None:
        url = f"{AIRTABLE_API_BASE}/{self._base_id}/" f"{quote(table, safe='')}/{record_id}"
        response = self._http().patch(
            url,
            json={"fields": fields, "typecast": False},
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise AirtableCRMWriteError(response.status_code, response.text)


def _append_block(*, current: str, block: str) -> str:
    """Append ``block`` to ``current``. Empty current = just the block.
    Otherwise newline-separator + block."""
    cleaned_current = (current or "").rstrip()
    if not cleaned_current:
        return block.rstrip()
    return f"{cleaned_current}\n\n{block.rstrip()}"


# ----------------------------------------------------------------- writer


class CrmDraftWriter:
    """Drafts Tasks + appends Pending Updates to Contacts / Accounts.

    Skips writes whose identity-resolution fails (unknown email, unknown
    account name) — the corresponding draft is logged and counted but
    not written. The audit row carries the count delta so misses are
    visible.
    """

    def __init__(
        self,
        *,
        airtable: AirtableCRMWriteClient,
        inbox_project_record_id: str,
        message_id: str,
    ) -> None:
        self._airtable = airtable
        self._inbox_project = inbox_project_record_id
        self._message_id = message_id

    def write(
        self,
        *,
        tasks: tuple[ExtractedTask, ...],
        contact_updates: tuple[ContactUpdate, ...],
        account_mentions: tuple[AccountMention, ...],
        run_timestamp_iso: str,
    ) -> DraftWriteResult:
        task_ids: list[str] = []
        for t in tasks:
            try:
                task_ids.append(
                    self._airtable.create_task(
                        self._task_fields(t, run_timestamp_iso=run_timestamp_iso)
                    )
                )
            except AirtableCRMWriteError as exc:
                log.warning(
                    "crm_updater.airtable_writer.task_failed status=%d body=%s",
                    exc.status_code,
                    exc.body[:200],
                )

        contact_count = 0
        for c in contact_updates:
            block = self._format_contact_block(update=c, run_timestamp_iso=run_timestamp_iso)
            try:
                resolved = self._airtable.find_contact_by_email(c.contact_email)
                if resolved is None:
                    log.info(
                        "crm_updater.airtable_writer.contact_not_found email=%s",
                        c.contact_email,
                    )
                    continue
                record_id, current = resolved
                self._airtable.append_contact_pending(
                    record_id=record_id, current=current, block=block
                )
                contact_count += 1
            except AirtableCRMWriteError as exc:
                log.warning(
                    "crm_updater.airtable_writer.contact_patch_failed status=%d",
                    exc.status_code,
                )

        account_count = 0
        for a in account_mentions:
            block = self._format_account_block(mention=a, run_timestamp_iso=run_timestamp_iso)
            try:
                resolved = self._airtable.find_account_by_name(a.account_name)
                if resolved is None:
                    log.info(
                        "crm_updater.airtable_writer.account_not_found name=%s",
                        a.account_name,
                    )
                    continue
                record_id, current = resolved
                self._airtable.append_account_pending(
                    record_id=record_id, current=current, block=block
                )
                account_count += 1
            except AirtableCRMWriteError as exc:
                log.warning(
                    "crm_updater.airtable_writer.account_patch_failed status=%d",
                    exc.status_code,
                )

        return DraftWriteResult(
            task_record_ids=tuple(task_ids),
            contact_updates_appended=contact_count,
            account_updates_appended=account_count,
        )

    # ------------------------------------------------------------------ helpers

    def _task_fields(self, task: ExtractedTask, *, run_timestamp_iso: str) -> dict:
        body: dict = {
            "Task Name": _truncate_task_name(task.title),
            "Source": "CRM Auto-updater",
            "Approval Status": "Drafted by Agent",
            "Status": "Open",
            "Project": [self._resolve_project(task)],
            "Source Reference": _gmail_message_url(self._message_id),
            "Task Type": "Task",
        }
        return body

    def _resolve_project(self, task: ExtractedTask) -> str:
        """Pick the best Project for ``task``.

        ADR 0047 fix-up (2026-05-11): the original implementation always
        used ``self._inbox_project``, ignoring the extractor's
        ``linked_account_name``. Real impact: every email-derived Task
        landed in Triage Inbox instead of its client's active Project.

        New behavior:
        1. If the task carries ``linked_account_name`` AND the writer
           client can find exactly one active Project for that Account,
           use that Project's record id.
        2. Otherwise fall back to the configured Triage Inbox so the
           Task is still triageable (no Project = invalid per the
           Tasks-required-Project rule in PRD §4.1).
        """
        name = (getattr(task, "linked_account_name", None) or "").strip()
        if name:
            try:
                resolved = self._airtable.find_active_project_for_account_name(name)
            except Exception:
                log.exception(
                    "crm_updater.airtable_writer.resolve_project_failed account=%s",
                    name,
                )
                resolved = None
            if resolved:
                return resolved
        return self._inbox_project

    def _format_contact_block(self, *, update: ContactUpdate, run_timestamp_iso: str) -> str:
        lines = [
            f"[{run_timestamp_iso} from {_gmail_message_url(self._message_id)}]",
            f"  Email: {update.contact_email}",
        ]
        if update.last_contact_date:
            lines.append(f"  Suggested Last Contact: {update.last_contact_date}")
        if update.next_followup_suggested:
            lines.append(f"  Suggested Next Followup: {update.next_followup_suggested}")
        if update.warmth_change != "unchanged":
            lines.append(f"  Warmth Change: {update.warmth_change}")
        if update.context_note:
            lines.append(f"  Context: {update.context_note}")
        return "\n".join(lines)

    def _format_account_block(self, *, mention: AccountMention, run_timestamp_iso: str) -> str:
        lines = [
            f"[{run_timestamp_iso} from {_gmail_message_url(self._message_id)}]",
            f"  Account: {mention.account_name}",
        ]
        if mention.context_note:
            lines.append(f"  Context: {mention.context_note}")
        if mention.new_contacts:
            lines.append(f"  Suggested New Contacts: {', '.join(mention.new_contacts)}")
        return "\n".join(lines)


def _truncate_task_name(s: str) -> str:
    s = (s or "").strip()
    return s[:197] + "..." if len(s) > 200 else (s or "(untitled)")


def _gmail_message_url(message_id: str) -> str:
    """Return a Gmail web-UI permalink for a Gmail API message id.

    ``/#all/<id>`` opens the message in the "All Mail" view, which
    works regardless of which folder the message is currently in
    (Inbox, processed, archived, etc.) — more reliable than
    ``/#inbox/<id>`` for messages the operator may have moved out
    of inbox after triage.

    Account selector ``u/0`` targets the primary signed-in account.
    A Workspace user with one account sees the message immediately;
    a multi-account user may need to switch accounts in Gmail first.
    """
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"
