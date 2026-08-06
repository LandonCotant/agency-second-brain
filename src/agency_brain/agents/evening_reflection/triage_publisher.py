"""Pub/Sub publisher for REFLECT-mode extracted todos (ADR 0040 §6).

Wraps the Pub/Sub publish call so the dispatcher can hand off
``ExtractedTodo`` rows without owning Pub/Sub knowledge. Envelope shape
mirrors ``captures_materializer.dispatch.build_todo_triage_envelope``
so the Triage Agent's existing classifier handles them as ordinary
inbound signals — no Triage-side change needed.

Idempotency is downstream: the Triage Agent dedups on
``source_event_ref`` per ADR 0026, so a re-tick that emits the same
todo (same ``reflection_id`` + same normalized title) collapses to a
no-op at classify time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .extracted_writers import title_hash12
from .models import ExtractedTodo

log = logging.getLogger("agency_brain.agents.evening_reflection.triage_publisher")


class PubSubPublisherClient(Protocol):
    """Matches ``google.cloud.pubsub_v1.PublisherClient.publish``."""

    def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str = "",
        **attributes: str,
    ) -> Any: ...


@dataclass(frozen=True)
class PublishOutcome:
    source_event_ref: str
    published: bool
    error: str | None = None


def todo_source_event_ref(*, reflection_id: str, title_or_body: str) -> str:
    """Idempotent ``source_event_ref`` for an extracted todo (ADR 0040 §6).

    Triage Agent dedups on this column (ADR 0026) — a re-tick that emits
    the same todo collapses to a no-op there. Same hash function as
    ``decision_id_for`` / ``win_id_for`` so the contract is uniform.
    """
    return f"reflection-todo/{reflection_id}/{title_hash12(title_or_body)}"


def build_todo_envelope(
    todo: ExtractedTodo,
    *,
    reflection_id: str,
    sender_email: str,
    now: datetime | None = None,
) -> dict:
    """Build the Pub/Sub envelope for a single extracted todo.

    Mirrors ``captures_materializer.dispatch.build_todo_triage_envelope``:
    ``source='airtable'`` so a future query can reconcile reflection-extracted
    todos with the ``triaged_items`` rows Triage produces.
    """
    n = now or datetime.now(UTC)
    body = todo.body
    subject = body if len(body) <= 80 else body[:79].rstrip() + "…"
    return {
        "source": "airtable",
        "source_url": "",
        "source_event_ref": todo_source_event_ref(
            reflection_id=reflection_id,
            title_or_body=body,
        ),
        "sender": sender_email,
        "subject": subject,
        "body": body,
        "ingested_at": n.isoformat(),
        "aspects": ["reflection_todo"],
    }


class ReflectTriagePublisher:
    """Publishes one ``ExtractedTodo`` envelope per call to ``asb-triage-input``."""

    def __init__(
        self,
        *,
        publisher: PubSubPublisherClient,
        topic_path: str,
        sender_email: str = "owner@example.com",
    ) -> None:
        self._publisher = publisher
        self._topic_path = topic_path
        self._sender_email = sender_email

    def publish(
        self,
        todo: ExtractedTodo,
        *,
        reflection_id: str,
        now: datetime | None = None,
    ) -> PublishOutcome:
        envelope = build_todo_envelope(
            todo,
            reflection_id=reflection_id,
            sender_email=self._sender_email,
            now=now,
        )
        ordering_key = envelope["source_event_ref"]
        try:
            data = json.dumps(envelope).encode("utf-8")
            future = self._publisher.publish(self._topic_path, data, ordering_key=ordering_key)
            if hasattr(future, "result"):
                future.result(timeout=30)
            return PublishOutcome(source_event_ref=ordering_key, published=True)
        except Exception as exc:  # — dispatcher aggregates
            log.exception(
                "evening_reflection.triage_publisher.publish_failed source_event_ref=%s",
                ordering_key,
            )
            return PublishOutcome(
                source_event_ref=ordering_key,
                published=False,
                error=f"{type(exc).__name__}: {exc}",
            )


__all__ = [
    "PublishOutcome",
    "PubSubPublisherClient",
    "ReflectTriagePublisher",
    "build_todo_envelope",
    "todo_source_event_ref",
]
