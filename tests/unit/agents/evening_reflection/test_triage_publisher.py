"""Unit tests for ADR 0040 §6 triage_publisher — envelope shape +
idempotent source_event_ref + publish error handling."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agency_brain.agents.evening_reflection.models import ExtractedTodo
from agency_brain.agents.evening_reflection.triage_publisher import (
    ReflectTriagePublisher,
    build_todo_envelope,
    todo_source_event_ref,
)

# --------------------------------------------------- source_event_ref


def test_todo_source_event_ref_uses_title_hash12():
    ref = todo_source_event_ref(
        reflection_id="r-1",
        title_or_body="Email Alice about the brief",
    )
    assert ref.startswith("reflection-todo/r-1/")
    # Trailing 12-char hex hash from title_hash12.
    suffix = ref.split("/")[-1]
    assert len(suffix) == 12


def test_todo_source_event_ref_stable_across_trivial_variation():
    a = todo_source_event_ref(reflection_id="r-1", title_or_body="Hire Alice")
    b = todo_source_event_ref(reflection_id="r-1", title_or_body="Hire Alice.")
    assert a == b


# --------------------------------------------------- build_todo_envelope


def test_build_todo_envelope_shape_matches_captures_pattern():
    """Same source/source_event_ref/aspects shape captures-materializer's
    todo envelope uses, so the Triage classifier handles it as one
    more inbound signal source — no Triage-side change needed."""
    todo = ExtractedTodo(body="Send the brief by Friday", source_voice_note_id="captures-recA")
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    env = build_todo_envelope(
        todo,
        reflection_id="r-77",
        sender_email="owner@example.com",
        now=now,
    )
    assert env["source"] == "airtable"
    assert env["sender"] == "owner@example.com"
    assert env["body"] == "Send the brief by Friday"
    assert env["aspects"] == ["reflection_todo"]
    assert env["ingested_at"] == now.isoformat()
    assert env["source_event_ref"].startswith("reflection-todo/r-77/")


def test_build_todo_envelope_truncates_long_subject():
    long_body = "x" * 200
    env = build_todo_envelope(
        ExtractedTodo(body=long_body, source_voice_note_id=None),
        reflection_id="r-1",
        sender_email="a@b.com",
    )
    # Subject capped at 80 chars.
    assert len(env["subject"]) <= 80
    # Body is full content.
    assert env["body"] == long_body


# --------------------------------------------------- publisher


@dataclass
class _FakePublisher:
    publishes: list = field(default_factory=list)
    raise_on_publish: bool = False

    def publish(self, topic, data, ordering_key="", **attrs):
        if self.raise_on_publish:
            raise RuntimeError("pubsub down")
        self.publishes.append((topic, data, ordering_key))

        class _Fut:
            def result(self_inner, timeout=None):
                return None

        return _Fut()


def test_publisher_publishes_envelope_with_ordering_key():
    pub = _FakePublisher()
    rp = ReflectTriagePublisher(
        publisher=pub,
        topic_path="projects/p/topics/asb-triage-input",
        sender_email="owner@example.com",
    )
    out = rp.publish(
        ExtractedTodo(body="Email Alice", source_voice_note_id="captures-recA"),
        reflection_id="r-1",
    )
    assert out.published is True
    assert out.error is None
    assert len(pub.publishes) == 1
    topic, data, ordering_key = pub.publishes[0]
    assert topic == "projects/p/topics/asb-triage-input"
    payload = json.loads(data.decode("utf-8"))
    assert payload["body"] == "Email Alice"
    assert ordering_key.startswith("reflection-todo/r-1/")
    assert ordering_key == out.source_event_ref
    # Ordering key matches the source_event_ref so Pub/Sub serializes
    # repeated todo emissions for the same reflection-id+title.


def test_publisher_returns_failure_outcome_on_pubsub_error():
    pub = _FakePublisher(raise_on_publish=True)
    rp = ReflectTriagePublisher(
        publisher=pub,
        topic_path="projects/p/topics/asb-triage-input",
    )
    out = rp.publish(
        ExtractedTodo(body="Email Alice", source_voice_note_id=None),
        reflection_id="r-1",
    )
    assert out.published is False
    assert out.error is not None
    assert "pubsub down" in out.error
