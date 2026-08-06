"""Unit tests for ChatWebhookClient (ADR 0023)."""

from __future__ import annotations

import pytest
from agency_brain.routing.channels.chat import (
    ChatPostResult,
    ChatRejectionError,
    ChatSendError,
    ChatWebhookClient,
)


class _RecordingPoster:
    """In-memory HttpPoster that records calls and replays canned responses."""

    def __init__(self, responses: list[tuple[int, str] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, float]] = []

    def post_json(self, url: str, payload: dict, *, timeout_s: float) -> tuple[int, str]:
        self.calls.append((url, payload, timeout_s))
        if not self._responses:
            raise AssertionError("no canned response for call")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_send_2xx_returns_ok_result() -> None:
    poster = _RecordingPoster([(200, '{"name": "spaces/AAAA/messages/BBBB"}')])
    client = ChatWebhookClient(
        "https://chat.googleapis.com/v1/spaces/X/messages?key=K&token=T",
        http=poster,
    )

    result = client.send("hello")

    assert isinstance(result, ChatPostResult)
    assert result.ok is True
    assert result.status == 200
    assert "spaces/AAAA" in result.response_body
    assert poster.calls[0][1] == {"text": "hello"}


def test_send_400_raises_chat_rejection() -> None:
    poster = _RecordingPoster([(400, '{"error":{"message":"bad payload"}}')])
    client = ChatWebhookClient("https://example/webhook", http=poster)

    with pytest.raises(ChatRejectionError) as exc:
        client.send("hello")
    assert exc.value.status == 400
    assert "bad payload" in exc.value.body


def test_send_500_raises_chat_send_error() -> None:
    poster = _RecordingPoster([(503, "service unavailable")])
    client = ChatWebhookClient("https://example/webhook", http=poster)

    with pytest.raises(ChatSendError, match="HTTP 503"):
        client.send("hello")


def test_network_failure_wraps_in_chat_send_error() -> None:
    poster = _RecordingPoster([TimeoutError("connection reset")])
    client = ChatWebhookClient("https://example/webhook", http=poster)

    with pytest.raises(ChatSendError, match="TimeoutError"):
        client.send("hello")


def test_empty_text_rejected_at_call_site() -> None:
    poster = _RecordingPoster([])
    client = ChatWebhookClient("https://example/webhook", http=poster)

    with pytest.raises(ValueError, match="non-empty"):
        client.send("")
    with pytest.raises(ValueError, match="non-empty"):
        client.send("   \n\t")


def test_constructor_rejects_empty_url() -> None:
    poster = _RecordingPoster([])
    with pytest.raises(ValueError, match="webhook_url"):
        ChatWebhookClient("", http=poster)
