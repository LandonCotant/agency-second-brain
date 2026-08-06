"""Brag Spotter Sunday-evening digest dispatch (ADR 0043 §6).

Two channels:

- **Chat card** via the existing `second-brain-gchat-webhook` secret
  (lifted from `routing.channels.chat`). Terse: count of wins + 'see
  Gmail draft' nudge.
- **Gmail draft** via `asb-agent-triage-sa` impersonation
  (`gmail.compose` DWD scope, ADR 0027 / 0029 — no new scope). Body is
  the LLM `commentary` field + a bulleted list of all wins for the
  week (both Brag-Spotter-extracted and Reflection-v2-extracted).

Per-channel try/except so a Chat outage doesn't break Gmail and vice
versa. Mirrors `routing.fanout_main.RoutingFanoutAgent` posture.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .models import ExistingWinRow, WinCandidate

log = logging.getLogger("agency_brain.agents.brag_spotter.digest")


class ChatClient(Protocol):
    """`ChatWebhookClient.send(text)` shape."""

    def send(self, text: str) -> ChatPostResultLike: ...


class ChatPostResultLike(Protocol):
    ok: bool
    status: int


class GmailDraftClient(Protocol):
    """`morning_brief.gmail_drafts_client.GmailDraftsClient.draft` shape."""

    def draft(self, *, recipient_email: str, subject: str, body_markdown: str) -> str: ...


@dataclass(frozen=True)
class DigestDispatchResult:
    chat_status: int | None
    gmail_draft_id: str | None
    chat_error: str | None = None
    gmail_error: str | None = None


def format_chat_card(*, week_of: date, total_wins: int) -> str:
    """Sunday-evening Chat notification — terse by design.

    The Gmail draft is the actual digest UI; Chat is just the nudge.
    """
    week_human = week_of.strftime("%b %d, %Y")
    if total_wins == 0:
        return (
            f"*Weekly wins — week of {week_human}*\n"
            "Quiet week — no wins flagged. Worth pausing on what *did* go "
            "well that wasn't loud enough to flag. See Gmail draft."
        )
    plural = "win" if total_wins == 1 else "wins"
    return (
        f"*Weekly wins — week of {week_human}*\n"
        f"{total_wins} {plural} this week. See Gmail draft for the full digest."
    )


def format_gmail_body(
    *,
    week_of: date,
    commentary: str,
    new_candidates: Iterable[WinCandidate],
    existing_wins: Iterable[ExistingWinRow],
) -> str:
    """Compose the Gmail draft body.

    Layout:
      1. Commentary paragraph (LLM-generated narrative).
      2. Newly-flagged wins (this Sunday's run) — title + summary +
         evidence links.
      3. Already-captured wins (from earlier in the week, e.g.
         Reflection-extracted) — title only, for context.

    On a quiet week (no candidates AND no existing wins), section 2 + 3
    are elided and the commentary stands alone.
    """
    week_human = week_of.strftime("%B %d, %Y")
    parts: list[str] = []
    parts.append(f"# Weekly wins — week of {week_human}\n")
    parts.append(commentary.strip())

    candidates_list = list(new_candidates)
    if candidates_list:
        parts.append("\n## Flagged this week\n")
        for c in candidates_list:
            line = f"- **{c.title}**"
            if c.summary:
                line += f" — {c.summary}"
            if c.evidence_links:
                links = ", ".join(c.evidence_links)
                line += f" _(evidence: {links})_"
            line += f" `[{c.source_kind}:{c.source_id}]`"
            parts.append(line)

    existing_list = list(existing_wins)
    if existing_list:
        parts.append("\n## Already captured earlier this week\n")
        for w in existing_list:
            parts.append(f"- {w.title} `[{w.source_kind}]`")

    if not candidates_list and not existing_list:
        parts.append(
            "\n*(No structured wins flagged. The week's reflection prose "
            "may still hold ground worth revisiting.)*"
        )

    return "\n".join(parts)


def format_gmail_subject(*, week_of: date, total_wins: int) -> str:
    week_human = week_of.strftime("%b %d")
    if total_wins == 0:
        return f"Weekly wins — week of {week_human} (quiet week)"
    plural = "win" if total_wins == 1 else "wins"
    return f"Weekly wins — week of {week_human} ({total_wins} {plural})"


class DigestDispatcher:
    """Sends the Chat card + Gmail draft (per-channel try/except)."""

    def __init__(
        self,
        *,
        chat: ChatClient | None,
        gmail: GmailDraftClient | None,
    ) -> None:
        self._chat = chat
        self._gmail = gmail

    def dispatch(
        self,
        *,
        recipient_email: str,
        week_of: date,
        commentary: str,
        new_candidates: list[WinCandidate],
        existing_wins: list[ExistingWinRow],
    ) -> DigestDispatchResult:
        total_wins = len(new_candidates) + len(existing_wins)

        chat_status: int | None = None
        chat_error: str | None = None
        if self._chat is not None:
            try:
                result = self._chat.send(format_chat_card(week_of=week_of, total_wins=total_wins))
                chat_status = int(result.status)
            except Exception as exc:
                chat_error = f"{type(exc).__name__}: {exc}"
                log.exception("brag_spotter.digest.chat_failed")

        gmail_draft_id: str | None = None
        gmail_error: str | None = None
        if self._gmail is not None:
            try:
                gmail_draft_id = self._gmail.draft(
                    recipient_email=recipient_email,
                    subject=format_gmail_subject(week_of=week_of, total_wins=total_wins),
                    body_markdown=format_gmail_body(
                        week_of=week_of,
                        commentary=commentary,
                        new_candidates=new_candidates,
                        existing_wins=existing_wins,
                    ),
                )
            except Exception as exc:
                gmail_error = f"{type(exc).__name__}: {exc}"
                log.exception("brag_spotter.digest.gmail_failed")

        return DigestDispatchResult(
            chat_status=chat_status,
            gmail_draft_id=gmail_draft_id,
            chat_error=chat_error,
            gmail_error=gmail_error,
        )
