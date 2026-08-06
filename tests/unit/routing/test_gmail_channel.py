"""Unit tests for routing.channels.gmail (ADR 0032)."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pytest
from agency_brain.routing.channels.gmail import (
    GmailDraftClient,
    GmailDraftError,
    GmailDraftRejectionError,
)

# ----------------------------------------------------------- googleapiclient stubs


class _FakeDrafts:
    def __init__(self, response, *, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc
        self.last_user_id: str = ""
        self.last_body: dict = {}

    def create(self, *, userId: str, body: dict):
        self.last_user_id = userId
        self.last_body = body
        return self

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._response


class _FakeUsers:
    def __init__(self, response, *, raise_exc: Exception | None = None):
        self._drafts = _FakeDrafts(response, raise_exc=raise_exc)

    def drafts(self):
        return self._drafts


class _FakeService:
    def __init__(self, response, *, raise_exc: Exception | None = None):
        self._users = _FakeUsers(response, raise_exc=raise_exc)

    def users(self):
        return self._users


@dataclass
class _FakeFactory:
    response: dict = field(default_factory=lambda: {"id": "r-100"})
    raise_exc: Exception | None = None
    last_subject: str = ""
    service: _FakeService | None = None

    def build(self, subject: str):
        self.last_subject = subject
        self.service = _FakeService(self.response, raise_exc=self.raise_exc)
        return self.service


class _FakeHttpError(Exception):
    """Mimics googleapiclient.errors.HttpError minimum surface."""

    def __init__(self, status: int, content: bytes = b""):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()
        self.content = content
        self.status_code = status


# ---------------------------------------------------------------- success paths


def test_draft_returns_ok_result_with_id() -> None:
    factory = _FakeFactory(response={"id": "r-42"})
    client = GmailDraftClient(service_factory=factory)

    result = client.draft(
        recipient_email="owner@example.com",
        subject="[CRITICAL] reply_today — gmail",
        body_markdown="Severity: CRITICAL\n\nReasoning here.",
    )

    assert result.ok is True
    assert result.draft_id == "r-42"
    assert result.threaded is False
    assert factory.last_subject == "owner@example.com"


def test_draft_payload_is_base64_rfc5322() -> None:
    factory = _FakeFactory()
    client = GmailDraftClient(service_factory=factory)
    client.draft(
        recipient_email="owner@example.com",
        subject="[CRITICAL] payload-shape-test",
        body_markdown="body line",
    )
    body = factory.service._users._drafts.last_body  # type: ignore[union-attr]
    assert "message" in body
    assert "raw" in body["message"]
    decoded = base64.urlsafe_b64decode(body["message"]["raw"]).decode("utf-8")
    assert "To: owner@example.com" in decoded
    assert "Subject: [CRITICAL] payload-shape-test" in decoded
    assert "body line" in decoded


def test_draft_with_thread_id_threads_in_payload() -> None:
    factory = _FakeFactory(response={"id": "r-99"})
    client = GmailDraftClient(service_factory=factory)

    result = client.draft(
        recipient_email="owner@example.com",
        subject="[CRITICAL] threaded test",
        body_markdown="body",
        thread_id="thread-abc",
    )

    body = factory.service._users._drafts.last_body  # type: ignore[union-attr]
    assert body["message"]["threadId"] == "thread-abc"
    assert result.threaded is True


def test_draft_without_thread_id_omits_threadid_field() -> None:
    factory = _FakeFactory(response={"id": "r-99"})
    client = GmailDraftClient(service_factory=factory)
    client.draft(
        recipient_email="x@y.com",
        subject="s",
        body_markdown="b",
    )
    body = factory.service._users._drafts.last_body  # type: ignore[union-attr]
    assert "threadId" not in body["message"]


# -------------------------------------------------------------- error handling


def test_draft_4xx_raises_permanent_rejection() -> None:
    factory = _FakeFactory(raise_exc=_FakeHttpError(400, b"bad recipient"))
    client = GmailDraftClient(service_factory=factory)

    with pytest.raises(GmailDraftRejectionError) as excinfo:
        client.draft(
            recipient_email="x@y.com",
            subject="s",
            body_markdown="b",
        )

    assert excinfo.value.status == 400


def test_draft_5xx_raises_transient_error() -> None:
    factory = _FakeFactory(raise_exc=_FakeHttpError(503, b"unavailable"))
    client = GmailDraftClient(service_factory=factory)

    with pytest.raises(GmailDraftError):
        client.draft(
            recipient_email="x@y.com",
            subject="s",
            body_markdown="b",
        )


def test_draft_unexpected_response_shape_raises_transient() -> None:
    factory = _FakeFactory(response={"unexpected": "shape"})
    client = GmailDraftClient(service_factory=factory)

    with pytest.raises(GmailDraftError):
        client.draft(
            recipient_email="x@y.com",
            subject="s",
            body_markdown="b",
        )


def test_draft_network_error_raises_transient() -> None:
    factory = _FakeFactory(raise_exc=ConnectionError("pipe broken"))
    client = GmailDraftClient(service_factory=factory)

    with pytest.raises(GmailDraftError):
        client.draft(
            recipient_email="x@y.com",
            subject="s",
            body_markdown="b",
        )


# -------------------------------------------------------------- input validation


def test_draft_empty_recipient_raises() -> None:
    client = GmailDraftClient(service_factory=_FakeFactory())
    with pytest.raises(ValueError, match="recipient_email"):
        client.draft(recipient_email="", subject="s", body_markdown="b")


def test_draft_empty_subject_raises() -> None:
    client = GmailDraftClient(service_factory=_FakeFactory())
    with pytest.raises(ValueError, match="subject"):
        client.draft(recipient_email="x@y.com", subject="   ", body_markdown="b")


def test_draft_empty_body_raises() -> None:
    client = GmailDraftClient(service_factory=_FakeFactory())
    with pytest.raises(ValueError, match="body"):
        client.draft(recipient_email="x@y.com", subject="s", body_markdown="")
