"""Google Chat incoming-webhook adapter for WS-D fan-out (ADR 0023).

The webhook URL is bearer-token-equivalent and lives in Secret Manager
(`second-brain-gchat-webhook`). The fanout job's SA is the only principal
with `secretAccessor` on it. The client itself is HTTP-only and has no
GCP dependencies, which keeps unit tests fast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


class ChatSendError(RuntimeError):
    """Transient failure: network, 5xx, timeout. Caller should retry."""


class ChatRejectionError(RuntimeError):
    """Permanent failure: 4xx response from Chat. Caller should not retry.

    Includes the HTTP status and response body for the audit row.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Chat webhook rejected: HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class HttpPoster(Protocol):
    """Minimal HTTP surface — production uses ``urllib.request.urlopen``.

    Tests inject a fake that records the payload and returns canned
    (status, body) tuples.
    """

    def post_json(self, url: str, payload: dict, *, timeout_s: float) -> tuple[int, str]: ...


@dataclass(frozen=True)
class ChatPostResult:
    ok: bool
    status: int
    response_body: str


class ChatWebhookClient:
    """POST a text message to a Google Chat space via incoming webhook.

    The Chat incoming-webhook payload shape is documented at
    https://developers.google.com/workspace/chat/quickstart/webhooks; for
    our purposes a single-key ``{"text": "..."}`` payload is enough. We
    deliberately avoid Card v2 / app-style features to stay compatible
    with the simplest webhook setup.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        http: HttpPoster,
        timeout_s: float = 10.0,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook_url must be non-empty")
        self._url = webhook_url
        self._http = http
        self._timeout_s = timeout_s

    def send(self, text: str) -> ChatPostResult:
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        payload = {"text": text}
        try:
            status, body = self._http.post_json(self._url, payload, timeout_s=self._timeout_s)
        except Exception as exc:
            raise ChatSendError(f"webhook POST failed: {type(exc).__name__}: {exc}") from exc

        if 200 <= status < 300:
            return ChatPostResult(ok=True, status=status, response_body=body)
        if 400 <= status < 500:
            raise ChatRejectionError(status, body)
        raise ChatSendError(f"webhook returned HTTP {status}: {body[:200]}")


# --------------------------------------------------------------- production HTTP


class UrllibPoster:
    """Production HttpPoster backed by ``urllib.request``.

    Kept out of tests by injecting a fake instead. No requests/httpx
    dependency — urllib ships with CPython and is sufficient for one
    POST per dispatch.
    """

    def post_json(self, url: str, payload: dict, *, timeout_s: float) -> tuple[int, str]:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        data = json.dumps(payload).encode("utf-8")
        # URL is the Chat incoming-webhook URL from Secret Manager — always
        # https://chat.googleapis.com/... — not user-controlled. urlopen's
        # scheme audit (S310) is a false positive for this caller.
        req = Request(  # noqa: S310
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: S110
                # Body read on an HTTPError can fail for many reasons
                # (already consumed, no body, decoding); we still want the
                # status code, so swallow and return an empty body.
                pass
            return exc.code, body
