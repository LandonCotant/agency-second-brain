"""Unit tests for the Gmail drafts client."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pytest
from agency_brain.agents.morning_brief.gmail_drafts_client import (
    GmailDraftsClient,
)


class _FakeDrafts:
    def __init__(self, response):
        self._response = response
        self.last_body: dict = {}

    def create(self, *, userId: str, body: dict):
        self.last_user_id = userId
        self.last_body = body
        return self

    def execute(self):
        return self._response


class _FakeUsers:
    def __init__(self, response):
        self._drafts = _FakeDrafts(response)

    def drafts(self):
        return self._drafts


class _FakeService:
    def __init__(self, response):
        self._users = _FakeUsers(response)

    def users(self):
        return self._users


@dataclass
class _FakeFactory:
    response: dict = field(default_factory=lambda: {"id": "r-100"})
    last_subject: str = ""
    service: _FakeService | None = None

    def build(self, subject: str):
        self.last_subject = subject
        self.service = _FakeService(self.response)
        return self.service


def test_draft_returns_id_on_success():
    factory = _FakeFactory(response={"id": "r-42"})
    client = GmailDraftsClient(service_factory=factory)
    draft_id = client.draft(
        recipient_email="owner@example.com",
        subject="Morning Brief — Tue, May 05, 2026",
        body_markdown="# Brief\n\nHi.",
    )
    assert draft_id == "r-42"
    assert factory.last_subject == "owner@example.com"


def test_draft_body_is_base64_email_message():
    factory = _FakeFactory()
    client = GmailDraftsClient(service_factory=factory)
    client.draft(
        recipient_email="owner@example.com",
        subject="Morning Brief — Tue, May 05, 2026",
        body_markdown="# Brief\n\nHi.",
    )
    body = factory.service._users._drafts.last_body  # type: ignore[union-attr]
    assert "message" in body
    assert "raw" in body["message"]
    decoded = base64.urlsafe_b64decode(body["message"]["raw"]).decode("utf-8")
    assert "To: owner@example.com" in decoded
    assert "Subject: Morning Brief" in decoded
    assert "# Brief" in decoded


def test_draft_raises_on_unexpected_response():
    factory = _FakeFactory(response={"unexpected": "shape"})
    client = GmailDraftsClient(service_factory=factory)
    with pytest.raises(RuntimeError):
        client.draft(
            recipient_email="x@y.com",
            subject="s",
            body_markdown="b",
        )
