"""Tests for ``GmailClient`` — query shape + label-apply discipline."""

from __future__ import annotations

import base64
from typing import Any

from agency_brain.agents.crm_updater.gmail_client import (
    _FORBIDDEN_REMOVE_LABEL_IDS,
    GmailClient,
    _parse_raw_message,
)


class _StubFactory:
    def __init__(self, *, service: Any) -> None:
        self._service = service
        self.last_subject: str | None = None

    def build(self, subject: str) -> Any:
        self.last_subject = subject
        return self._service


class _StubService:
    """Mimics googleapiclient's chained API: service.users().messages().list(...).execute()"""

    def __init__(
        self,
        *,
        list_response: dict | None = None,
        get_response: dict | None = None,
        labels_list: dict | None = None,
        label_create: dict | None = None,
    ) -> None:
        self.list_response = list_response or {}
        self.get_response = get_response or {}
        self.labels_list = labels_list or {"labels": []}
        self.label_create = label_create or {}
        self.list_calls: list[dict] = []
        self.modify_calls: list[dict] = []
        self.label_create_calls: list[dict] = []
        self.get_calls: list[dict] = []

    # Top-level
    def users(self) -> _StubService:
        return self

    # Resource selectors
    def messages(self) -> _StubService:
        return self

    def labels(self) -> _StubService:
        return self

    # Operations — return objects with .execute()
    def list(self, **kwargs) -> _StubExecuteable:
        # `users().messages().list` and `users().labels().list` both land here.
        if "userId" in kwargs and "q" in kwargs:
            self.list_calls.append(kwargs)
            return _StubExecuteable(self.list_response)
        return _StubExecuteable(self.labels_list)

    def get(self, **kwargs) -> _StubExecuteable:
        self.get_calls.append(kwargs)
        return _StubExecuteable(self.get_response)

    def modify(self, **kwargs) -> _StubExecuteable:
        self.modify_calls.append(kwargs)
        return _StubExecuteable({})

    def create(self, **kwargs) -> _StubExecuteable:
        self.label_create_calls.append(kwargs)
        return _StubExecuteable(self.label_create or {"id": "Label_test"})


class _StubExecuteable:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


def _make_client(*, service: _StubService) -> tuple[GmailClient, _StubFactory, _StubFactory]:
    ro = _StubFactory(service=service)
    mw = _StubFactory(service=service)
    return (
        GmailClient(
            readonly_factory=ro,
            modify_factory=mw,
            subject="owner@example.com",
        ),
        ro,
        mw,
    )


def test_list_query_includes_label() -> None:
    """LOAD-BEARING: dropping the `label:` filter would cause the agent
    to read the entire inbox (ADR 0047 §threat-model 3)."""
    service = _StubService(
        list_response={
            "messages": [{"id": "m1", "threadId": "t1"}],
        }
    )
    client, ro, _ = _make_client(service=service)
    result = client.list_labeled_messages(label="secondbrain")
    assert len(service.list_calls) == 1
    q = service.list_calls[0]["q"]
    assert "label:secondbrain" in q
    # Also requires the dedup-label exclusion so processed messages are skipped.
    assert "-label:secondbrain-processed" in q
    assert ro.last_subject == "owner@example.com"
    assert len(result.messages) == 1
    assert result.messages[0].message_id == "m1"


def test_list_returns_empty_on_no_messages() -> None:
    service = _StubService(list_response={})
    client, _, _ = _make_client(service=service)
    result = client.list_labeled_messages(label="secondbrain")
    assert result.messages == ()


def test_apply_label_does_not_remove_system_labels() -> None:
    """The body sent to messages.modify must include `addLabelIds` and
    NOT `removeLabelIds`. Removing system labels (INBOX, IMPORTANT, etc.)
    is an explicit rejected vector (ADR 0047 §threat-model 2)."""
    service = _StubService(
        labels_list={
            "labels": [
                {"id": "Label_processed", "name": "secondbrain-processed"},
            ]
        }
    )
    client, _, mw = _make_client(service=service)
    client.apply_label(message_id="m1", label_name="secondbrain-processed")
    assert len(service.modify_calls) == 1
    body = service.modify_calls[0]["body"]
    assert body == {"addLabelIds": ["Label_processed"]}
    assert "removeLabelIds" not in body


def test_apply_label_creates_label_if_missing() -> None:
    service = _StubService(
        labels_list={"labels": []},  # no existing labels
        label_create={"id": "Label_new"},
    )
    client, _, _ = _make_client(service=service)
    client.apply_label(message_id="m1", label_name="secondbrain-processed")
    assert len(service.label_create_calls) == 1
    create_body = service.label_create_calls[0]["body"]
    assert create_body["name"] == "secondbrain-processed"
    # Modify call uses the newly-created label id.
    assert service.modify_calls[0]["body"]["addLabelIds"] == ["Label_new"]


def test_forbidden_remove_label_ids_includes_system_labels() -> None:
    """Sanity: this constant exists and includes INBOX, etc. The runtime
    code never constructs `removeLabelIds`; this set documents the
    semantic boundary for future edits."""
    assert "INBOX" in _FORBIDDEN_REMOVE_LABEL_IDS
    assert "SENT" in _FORBIDDEN_REMOVE_LABEL_IDS
    assert "IMPORTANT" in _FORBIDDEN_REMOVE_LABEL_IDS


# ----------------------------------------------------------------- raw parser


def _make_raw_email(*, subject: str, sender: str, recipient: str, body: str) -> str:
    raw = (
        f"Subject: {subject}\r\n"
        f"From: {sender}\r\n"
        f"To: {recipient}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def test_parse_raw_message_extracts_headers_and_body() -> None:
    raw = _make_raw_email(
        subject="Re: Q2 review",
        sender="Sarah Chen <sarah@example.com>",
        recipient="the operator <owner@example.com>",
        body="Following up on the Q2 review notes you sent.",
    )
    payload = {
        "id": "m1",
        "threadId": "t1",
        "raw": raw,
        "internalDate": "1715299200000",
        "labelIds": ["INBOX", "Label_secondbrain"],
    }
    msg = _parse_raw_message(payload)
    assert msg.message_id == "m1"
    assert msg.subject == "Re: Q2 review"
    assert msg.from_addr == "sarah@example.com"
    assert msg.to_addrs == ("owner@example.com",)
    assert "Following up" in msg.body_text
    assert msg.label_ids == ("INBOX", "Label_secondbrain")
    # No historyId in the test payload — must default to None, not raise.
    assert msg.history_id is None


def test_parse_raw_message_populates_history_id_when_present() -> None:
    """Regression: ``main.py`` reads ``full.history_id`` unconditionally.
    Before this fix the attribute didn't exist on the dataclass and the
    agent crashed on first prod fire after DWD propagation."""
    raw = _make_raw_email(
        subject="Hi",
        sender="bob@example.com",
        recipient="owner@example.com",
        body="Hi.",
    )
    payload = {
        "id": "m2",
        "threadId": "t2",
        "raw": raw,
        "internalDate": "1715299200000",
        "labelIds": [],
        "historyId": "19384733",
    }
    msg = _parse_raw_message(payload)
    assert msg.history_id == "19384733"


def test_parse_raw_message_empty_body_path_carries_history_id() -> None:
    """The empty-raw fallback path also needs the field populated; it
    constructs GmailMessage with its own kwarg set."""
    payload = {
        "id": "m3",
        "threadId": "t3",
        "raw": "",
        "labelIds": ["INBOX"],
        "historyId": "5",
    }
    msg = _parse_raw_message(payload)
    assert msg.body_text == ""
    assert msg.history_id == "5"
