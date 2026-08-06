"""Gmail draft adapter for WS-D fan-out (ADR 0032).

Wraps the existing `agents.morning_brief.gmail_drafts_client.GmailDraftsClient`
so the Morning Brief and the routing fan-out share one drafts.create call
site. ADR 0027 §2 invariant preserved — `asb-agent-triage-sa` remains the
only DWD-grantable SA; `asb-routing-sa` impersonates it via the
`serviceAccountTokenCreator` binding (PR 2 TF).

PRD §4.7 / ADR 0027 boundary stays intact at three layers:
  1. Workspace DWD scope grant: `gmail.compose` only
  2. `drafts_boundary_check.py` runtime audit (ADR 0027): allowlist ==
     `{gmail.compose, calendar.readonly}`
  3. `drafts_static_check.py` PR-gate (ADR 0027): blocks
     ``users.messages.send`` / ``users.messages.modify`` from landing
     in `src/`. This module never imports those.

Threading: when `thread_id` is provided on the dispatch input (Gmail
source only), the draft is created inside that thread. When absent,
the draft is a fresh email. Per ADR 0032, threadId is expected to flow
in via the WS-B PR-4 Gmail-to-Pub/Sub publisher once shipped; until
then drafts are always fresh.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

log = logging.getLogger("agency_brain.routing.channels.gmail")

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


class GmailDraftError(RuntimeError):
    """Transient failure: 5xx, network, timeout. Caller should retry next tick."""


class GmailDraftRejectionError(RuntimeError):
    """Permanent failure: 4xx (bad recipient, scope drift, malformed payload).

    Caller should not retry. Includes the HTTP status and response body
    for the audit row.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"gmail drafts.create rejected: HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class GmailServiceFactory(Protocol):
    """Builds a googleapiclient Gmail v1 service for a given subject.

    Production caller wires `common.dwd.DWDServiceFactory(scope=GMAIL_COMPOSE_SCOPE)`;
    tests inject a fake.
    """

    def build(self, subject: str) -> Any: ...


@dataclass(frozen=True)
class GmailDraftResult:
    ok: bool
    draft_id: str
    threaded: bool


class GmailDraftClient:
    """Creates a Gmail draft as the recipient (DWD-impersonated).

    Sole public method `draft(...)` returns `GmailDraftResult`. Errors
    are raised as `GmailDraftError` (transient) or
    `GmailDraftRejectionError` (permanent) — same dispatch semantics as
    the Chat adapter at `channels/chat.py`.
    """

    def __init__(self, *, service_factory: GmailServiceFactory) -> None:
        self._factory = service_factory

    def draft(
        self,
        *,
        recipient_email: str,
        subject: str,
        body_markdown: str,
        thread_id: str | None = None,
    ) -> GmailDraftResult:
        if not recipient_email:
            raise ValueError("recipient_email must be non-empty")
        if not subject or not subject.strip():
            raise ValueError("subject must be non-empty")
        if not body_markdown or not body_markdown.strip():
            raise ValueError("body_markdown must be non-empty")

        msg = EmailMessage()
        msg["To"] = recipient_email
        msg["From"] = recipient_email
        msg["Subject"] = subject
        msg.set_content(body_markdown)

        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        message_body: dict[str, Any] = {"raw": encoded}
        if thread_id:
            message_body["threadId"] = thread_id

        request_body = {"message": message_body}

        try:
            service = self._factory.build(recipient_email)
            # ONLY drafts().create() — never messages().send() / .modify().
            # The static check at scripts/drafts_static_check.py blocks those
            # call sites from this module.
            result = service.users().drafts().create(userId="me", body=request_body).execute()
        except Exception as exc:
            status = _http_status(exc)
            body = _http_body(exc)
            if status is not None and 400 <= status < 500:
                raise GmailDraftRejectionError(status, body) from exc
            raise GmailDraftError(
                f"gmail drafts.create failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(result, dict) or "id" not in result:
            raise GmailDraftError(f"gmail drafts.create returned unexpected shape: {result!r}")

        log.info(
            "gmail_draft: recipient=%s draft_id=%s threaded=%s",
            recipient_email,
            result["id"],
            thread_id is not None,
        )
        return GmailDraftResult(ok=True, draft_id=str(result["id"]), threaded=thread_id is not None)


def _http_status(exc: BaseException) -> int | None:
    """Extract a googleapiclient HttpError status, if present."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    if resp is not None:
        rs = getattr(resp, "status", None)
        if isinstance(rs, int):
            return rs
        if isinstance(rs, str) and rs.isdigit():
            return int(rs)
    return None


def _http_body(exc: BaseException) -> str:
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(content, str):
        return content
    return str(exc)
