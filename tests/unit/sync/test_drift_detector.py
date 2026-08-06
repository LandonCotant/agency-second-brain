"""Drift detector emits one Pub/Sub event per new column, never auto-applies."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agency_brain.sync.drift_detector import (
    DriftEvent,
    detect_new_columns,
    publish_drift,
)


class _FakeFuture:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    def result(self, timeout: int) -> str:
        return self._message_id


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic_path: str, data: bytes) -> _FakeFuture:
        self.published.append((topic_path, data))
        return _FakeFuture(f"msg-{len(self.published)}")


def test_no_drift_returns_empty():
    new = detect_new_columns(
        "Clients",
        observed_field_names=["Client Name", "HIPAA"],
        known_field_names=["Client Name", "HIPAA", "Stage"],
    )
    assert new == ()


def test_detects_single_new_column():
    new = detect_new_columns(
        "Clients",
        observed_field_names=["Client Name", "HIPAA", "Renewal Risk Score"],
        known_field_names=["Client Name", "HIPAA"],
    )
    assert new == ("Renewal Risk Score",)


def test_detects_multiple_new_columns_sorted():
    new = detect_new_columns(
        "Clients",
        observed_field_names=["Z col", "A col", "Client Name"],
        known_field_names=["Client Name"],
    )
    assert new == ("A col", "Z col")


def test_drift_event_serializes_to_pubsub_payload():
    event = DriftEvent(
        table_name="Clients",
        new_columns=("Renewal Risk Score",),
        detected_at=datetime(2026, 4, 25, 10, 30, tzinfo=UTC),
    )
    body = json.loads(event.to_message_bytes().decode("utf-8"))
    assert body["table"] == "Clients"
    assert body["new_columns"] == ["Renewal Risk Score"]
    assert body["detected_at"] == "2026-04-25T10:30:00+00:00"
    assert "Do NOT auto-apply" in body["recommended_action"]


def test_publish_drift_returns_message_id():
    publisher = _RecordingPublisher()
    event = DriftEvent(
        table_name="Projects",
        new_columns=("Color",),
        detected_at=datetime(2026, 4, 25, tzinfo=UTC),
    )
    message_id = publish_drift(publisher, "projects/p/topics/asb-schema-drift-alerts", event)

    assert message_id == "msg-1"
    assert len(publisher.published) == 1
    topic, body = publisher.published[0]
    assert topic == "projects/p/topics/asb-schema-drift-alerts"
    assert json.loads(body)["table"] == "Projects"
