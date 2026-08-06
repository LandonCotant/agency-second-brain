"""Gmail client for the CRM Auto-updater (ADR 0047).

Wraps the Gmail v1 API via DWD impersonation through ``common/dwd.py``.
Three operations:

  1. ``list_labeled_messages(label, since_history_id)`` — lists message
     IDs with the given label, optionally bounded by historyId for
     incremental runs. Critical: ``q=label:<label>`` is the load-bearing
     query bound — drop it and the agent reads the entire inbox.
  2. ``get_message(message_id)`` — fetches the full message (subject,
     from/to/cc, body, label IDs).
  3. ``apply_label(message_id, label_name)`` — adds the
     ``secondbrain-processed`` dedup label after successful drafting.

The static check at ``scripts/drafts_static_check.py`` continues to
forbid ``users.messages.send``, ``.trash``, ``.batchModify``. We use
``users.messages.modify`` ONLY to add label IDs — never to remove
``INBOX`` / ``IMPORTANT`` / system labels.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as email_default
from typing import Any, Protocol

from .models import GmailMessage, GmailMessageRef

log = logging.getLogger("agency_brain.agents.crm_updater.gmail_client")

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# Forbid removing system labels via users.messages.modify — only ADD
# the dedup label. The Gmail API allows arbitrary label changes via
# `addLabelIds` / `removeLabelIds`; we only construct `addLabelIds`.
_FORBIDDEN_REMOVE_LABEL_IDS: frozenset[str] = frozenset(
    {"INBOX", "IMPORTANT", "STARRED", "UNREAD", "SENT", "DRAFT", "SPAM", "TRASH"}
)


class GmailServiceFactory(Protocol):
    """Returns a googleapiclient Gmail v1 service for ``subject``."""

    def build(self, subject: str) -> Any: ...


@dataclass(frozen=True)
class GmailListResult:
    """Result of a label-scoped list call."""

    messages: tuple[GmailMessageRef, ...]
    next_page_token: str | None


class GmailClient:
    """Reads `secondbrain`-labeled mail; applies the processed label."""

    def __init__(
        self,
        *,
        readonly_factory: GmailServiceFactory,
        modify_factory: GmailServiceFactory,
        subject: str,
    ) -> None:
        self._readonly_factory = readonly_factory
        self._modify_factory = modify_factory
        self._subject = subject

    # -------------------------------------------------------------- list

    def list_labeled_messages(
        self,
        *,
        label: str,
        max_results: int = 50,
        since_history_id: str | None = None,
        page_token: str | None = None,
    ) -> GmailListResult:
        """List messages with the given label.

        Even though we have an optional ``since_history_id`` parameter
        (for future history-API integration), the v1 implementation
        relies on the label-scope + the dedup-label check at processing
        time, since the Gmail history API requires a watch+pubsub setup
        we explicitly chose against in ADR 0047.
        """
        service = self._readonly_factory.build(self._subject)
        query = self._build_query(label=label)
        request = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token,
            )
        )
        result = request.execute()
        if not isinstance(result, dict):
            return GmailListResult(messages=(), next_page_token=None)
        raw_messages = result.get("messages") or []
        messages = tuple(
            GmailMessageRef(
                message_id=str(m.get("id") or ""),
                thread_id=str(m.get("threadId") or ""),
            )
            for m in raw_messages
            if m.get("id")
        )
        return GmailListResult(
            messages=messages,
            next_page_token=result.get("nextPageToken") or None,
        )

    # --------------------------------------------------------------- get

    def get_message(self, *, message_id: str) -> GmailMessage:
        service = self._readonly_factory.build(self._subject)
        result = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
        return _parse_raw_message(result)

    # ------------------------------------------------------------ modify

    def apply_label(
        self,
        *,
        message_id: str,
        label_name: str,
    ) -> None:
        """Add a label to a message. Cannot remove labels (see
        ``_FORBIDDEN_REMOVE_LABEL_IDS``)."""
        service = self._modify_factory.build(self._subject)
        label_id = self._resolve_or_create_label(service, label_name)
        body = {"addLabelIds": [label_id]}  # NOTE: intentionally no removeLabelIds
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body=body,
        ).execute()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _build_query(*, label: str) -> str:
        """Build the Gmail q= query string. Load-bearing — must contain
        ``label:<label>`` per ADR 0047 §threat-model 3."""
        return f"label:{label} -label:secondbrain-processed"

    @staticmethod
    def _resolve_or_create_label(service: Any, label_name: str) -> str:
        # Look up the label_id; create if missing.
        labels_resp = service.users().labels().list(userId="me").execute()
        for raw in (labels_resp or {}).get("labels", []):
            if (raw or {}).get("name") == label_name:
                return str(raw["id"])
        # Create the label.
        body = {
            "name": label_name,
            "labelListVisibility": "labelHide",
            "messageListVisibility": "show",
        }
        created = service.users().labels().create(userId="me", body=body).execute()
        return str(created["id"])


# --------------------------------------------------------------------- helpers


def _parse_raw_message(api_payload: dict[str, Any]) -> GmailMessage:
    """Parse a `format=raw` Gmail get response into a ``GmailMessage``."""
    if not isinstance(api_payload, dict):
        raise ValueError("expected a dict from gmail.users.messages.get")
    raw = api_payload.get("raw") or ""
    if not raw:
        # Some accounts return base64url empty when format=raw and the body
        # was decrypted — fall back to the labelIds + headers from payload
        # to keep the agent operable.
        return GmailMessage(
            message_id=str(api_payload.get("id") or ""),
            thread_id=str(api_payload.get("threadId") or ""),
            subject="",
            from_addr="",
            to_addrs=(),
            cc_addrs=(),
            body_text="",
            received_at=datetime.now(UTC),
            label_ids=tuple(api_payload.get("labelIds") or ()),
            history_id=_history_id_from(api_payload),
        )
    raw_bytes = base64.urlsafe_b64decode(raw + "==")  # padding tolerant
    eml = message_from_bytes(raw_bytes, policy=email_default)

    subject = (eml.get("Subject") or "").strip()
    from_addr = _strip_name(eml.get("From") or "")
    to_addrs = tuple(_strip_name(a) for a in (eml.get_all("To") or []))
    cc_addrs = tuple(_strip_name(a) for a in (eml.get_all("Cc") or []))

    body_text = ""
    if eml.is_multipart():
        for part in eml.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
                    break
    else:
        payload = eml.get_payload(decode=True)
        if isinstance(payload, bytes):
            body_text = payload.decode(eml.get_content_charset() or "utf-8", errors="replace")

    return GmailMessage(
        message_id=str(api_payload.get("id") or ""),
        thread_id=str(api_payload.get("threadId") or ""),
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        body_text=body_text,
        received_at=_parse_internal_date(api_payload.get("internalDate")),
        label_ids=tuple(api_payload.get("labelIds") or ()),
        history_id=_history_id_from(api_payload),
    )


def _history_id_from(api_payload: dict[str, Any]) -> str | None:
    """Normalize ``historyId`` off a Gmail get response. The API returns
    a numeric string (e.g. ``"19384733"``); empty / missing → None."""
    raw = api_payload.get("historyId")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


_NAME_ANGLE_RE = re.compile(r"^[^<]*<([^>]+)>\s*$")


def _strip_name(addr: str) -> str:
    """Reduce ``"Sarah Chen <sarah@x.com>"`` → ``"sarah@x.com"``."""
    if not addr:
        return ""
    m = _NAME_ANGLE_RE.match(addr.strip())
    if m:
        return m.group(1).strip().lower()
    return addr.strip().lower()


def _parse_internal_date(internal_date: Any) -> datetime:
    if internal_date is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(int(internal_date) / 1000.0, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)
