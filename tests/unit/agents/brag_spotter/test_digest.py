"""Unit tests for the Sunday-evening digest formatters + dispatcher (ADR 0043 §6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from agency_brain.agents.brag_spotter.digest import (
    DigestDispatcher,
    format_chat_card,
    format_gmail_body,
    format_gmail_subject,
)
from agency_brain.agents.brag_spotter.models import ExistingWinRow, WinCandidate

# ---------------------------------------------------------------- chat card


def test_format_chat_card_quiet_week():
    card = format_chat_card(week_of=date(2026, 5, 4), total_wins=0)
    assert "May 04, 2026" in card
    assert "Quiet week" in card
    assert "Gmail draft" in card


def test_format_chat_card_singular_plural():
    one = format_chat_card(week_of=date(2026, 5, 4), total_wins=1)
    many = format_chat_card(week_of=date(2026, 5, 4), total_wins=3)
    assert "1 win" in one and "wins" not in one.split("1 win")[1].split("\n")[0]
    assert "3 wins" in many


# ---------------------------------------------------------------- gmail subject


def test_format_gmail_subject_quiet_week():
    subj = format_gmail_subject(week_of=date(2026, 5, 4), total_wins=0)
    assert "quiet week" in subj
    assert "May 04" in subj


def test_format_gmail_subject_pluralization():
    assert "1 win" in format_gmail_subject(week_of=date(2026, 5, 4), total_wins=1)
    assert "5 wins" in format_gmail_subject(week_of=date(2026, 5, 4), total_wins=5)


# ---------------------------------------------------------------- gmail body


def test_format_gmail_body_includes_commentary():
    body = format_gmail_body(
        week_of=date(2026, 5, 4),
        commentary="It was a week.",
        new_candidates=[],
        existing_wins=[],
    )
    assert "May 04, 2026" in body
    assert "It was a week." in body


def test_format_gmail_body_lists_new_candidates_with_evidence():
    body = format_gmail_body(
        week_of=date(2026, 5, 4),
        commentary="x",
        new_candidates=[
            WinCandidate(
                title="Closed Acme",
                summary="5-day loop",
                source_kind="decision",
                source_id="d-1",
                evidence_links=("https://x/y",),
            )
        ],
        existing_wins=[],
    )
    assert "Flagged this week" in body
    assert "Closed Acme" in body
    assert "5-day loop" in body
    assert "https://x/y" in body
    assert "[decision:d-1]" in body


def test_format_gmail_body_lists_existing_wins_separately():
    body = format_gmail_body(
        week_of=date(2026, 5, 4),
        commentary="x",
        new_candidates=[],
        existing_wins=[
            ExistingWinRow(
                win_id="w-1",
                title="Reflection-extracted",
                source_kind="reflection",
                source_id="r-1",
            )
        ],
    )
    assert "Already captured earlier this week" in body
    assert "Reflection-extracted" in body


def test_format_gmail_body_quiet_week_callout():
    body = format_gmail_body(
        week_of=date(2026, 5, 4),
        commentary="Quiet week.",
        new_candidates=[],
        existing_wins=[],
    )
    assert "No structured wins flagged" in body


# ---------------------------------------------------------------- dispatcher


@dataclass
class _FakeChatResult:
    ok: bool = True
    status: int = 200


@dataclass
class _FakeChatClient:
    sent_text: str | None = None
    raise_exc: Exception | None = None

    def send(self, text: str) -> _FakeChatResult:
        if self.raise_exc:
            raise self.raise_exc
        self.sent_text = text
        return _FakeChatResult()


@dataclass
class _FakeGmailClient:
    drafted: tuple[str, str, str] | None = None
    raise_exc: Exception | None = None
    canned_id: str = "r-99"

    def draft(self, *, recipient_email: str, subject: str, body_markdown: str) -> str:
        if self.raise_exc:
            raise self.raise_exc
        self.drafted = (recipient_email, subject, body_markdown)
        return self.canned_id


def test_dispatcher_sends_both_channels():
    chat = _FakeChatClient()
    gmail = _FakeGmailClient()
    dispatcher = DigestDispatcher(chat=chat, gmail=gmail)
    result = dispatcher.dispatch(
        recipient_email="owner@example.com",
        week_of=date(2026, 5, 4),
        commentary="x",
        new_candidates=[],
        existing_wins=[],
    )
    assert result.chat_status == 200
    assert result.gmail_draft_id == "r-99"
    assert chat.sent_text is not None
    assert gmail.drafted is not None
    assert gmail.drafted[0] == "owner@example.com"


def test_dispatcher_isolates_chat_failure_from_gmail():
    chat = _FakeChatClient(raise_exc=RuntimeError("network blip"))
    gmail = _FakeGmailClient()
    dispatcher = DigestDispatcher(chat=chat, gmail=gmail)
    result = dispatcher.dispatch(
        recipient_email="x@y.com",
        week_of=date(2026, 5, 4),
        commentary="x",
        new_candidates=[],
        existing_wins=[],
    )
    assert result.chat_status is None
    assert result.chat_error is not None
    assert "RuntimeError" in result.chat_error
    # Gmail still fired.
    assert result.gmail_draft_id == "r-99"


def test_dispatcher_isolates_gmail_failure_from_chat():
    chat = _FakeChatClient()
    gmail = _FakeGmailClient(raise_exc=RuntimeError("DWD failed"))
    dispatcher = DigestDispatcher(chat=chat, gmail=gmail)
    result = dispatcher.dispatch(
        recipient_email="x@y.com",
        week_of=date(2026, 5, 4),
        commentary="x",
        new_candidates=[],
        existing_wins=[],
    )
    assert result.gmail_draft_id is None
    assert result.gmail_error is not None
    # Chat still fired.
    assert result.chat_status == 200
