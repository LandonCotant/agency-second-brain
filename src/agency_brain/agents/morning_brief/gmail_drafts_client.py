"""Gmail Drafts writer for the Morning Brief.

Uses Workspace Domain-Wide Delegation impersonation (ADR 0027): the
Cloud Run Job runs as `asb-agent-triage-sa`, then mints a downscoped
credential `with_subject(recipient_email)` for
`https://www.googleapis.com/auth/gmail.compose`. Compose-only — cannot
send. PRD §4.7 boundary enforced at three layers:
  1. Workspace DWD scope grant: gmail.compose only
  2. drafts_boundary_check.py runtime audit (ADR 0027): allowlist ==
     {gmail.compose, calendar.readonly}
  3. drafts_static_check.py PR-gate (ADR 0027): blocks
     ``users.messages.send`` / ``users.messages.modify`` from landing
     in src/.

This module never imports `messages.send` / `messages.modify` and never
constructs the literal scope strings `gmail.send` / `gmail.modify`.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Any, Protocol

log = logging.getLogger("agency_brain.agents.morning_brief.gmail_drafts_client")

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


class GmailServiceFactory(Protocol):
    """Builds a googleapiclient Gmail v1 service for a given subject."""

    def build(self, subject: str) -> Any: ...


class GmailDraftsClient:
    """Drafts a plaintext email into the recipient's Drafts folder.

    Returns the Gmail draft id ("rXX..."). Body is plain text per the
    ADR 0029 v1 decision (markdown text — Gmail compose renders it
    legibly without HTML multipart).
    """

    def __init__(self, *, service_factory: GmailServiceFactory) -> None:
        self._factory = service_factory

    def draft(
        self,
        *,
        recipient_email: str,
        subject: str,
        body_markdown: str,
    ) -> str:
        msg = EmailMessage()
        msg["To"] = recipient_email
        msg["From"] = recipient_email
        msg["Subject"] = subject
        msg.set_content(body_markdown)

        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        request_body = {"message": {"raw": encoded}}

        service = self._factory.build(recipient_email)
        # NOTE: ONLY drafts().create() — never messages().send() / .modify().
        # The static check at scripts/drafts_static_check.py blocks those
        # call sites from this module.
        result = service.users().drafts().create(userId="me", body=request_body).execute()
        if not isinstance(result, dict) or "id" not in result:
            raise RuntimeError(
                f"gmail_drafts_client: unexpected drafts.create response shape: {result!r}"
            )
        log.info(
            "gmail_drafts_client: drafted brief for %s as draft_id=%s",
            recipient_email,
            result["id"],
        )
        return str(result["id"])
