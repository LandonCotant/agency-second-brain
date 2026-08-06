"""Unit tests for the notes ingestor's Pub/Sub publisher into asb-triage-input."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from agency_brain.agents.notes_ingestor.models import (
    DriveFile,
    ExtractionMethod,
    ExtractionResult,
    NoteFolder,
)
from agency_brain.agents.notes_ingestor.triage_publisher import (
    TriagePublisher,
    TriagePublishError,
)


@dataclass
class _FakeFuture:
    return_value: str = "msg-id-123"

    def result(self, timeout: int | None = None) -> str:
        return self.return_value


@dataclass
class _FakePublisher:
    captured: list[dict] = field(default_factory=list)
    raises: BaseException | None = None
    future: _FakeFuture = field(default_factory=_FakeFuture)

    def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str = "",
        **attributes,
    ):
        self.captured.append(
            {
                "topic": topic,
                "data": data,
                "ordering_key": ordering_key,
                "attributes": attributes,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.future


def _drive_file(folder: NoteFolder = NoteFolder.DEFAULT) -> DriveFile:
    return DriveFile(
        file_id="file-1",
        revision_id="rev-1",
        name="meeting.pdf",
        modified_time=datetime(2026, 5, 2, 11, 55, tzinfo=UTC),
        web_view_link="https://drive.google.com/file/d/file-1/view",
        folder=folder,
    )


def _extraction(markdown: str = "# meeting\n- note") -> ExtractionResult:
    return ExtractionResult(
        markdown=markdown,
        page_count=1,
        method=ExtractionMethod.GEMINI_FLASH,
        confidence=0.95,
    )


def test_publish_emits_envelope_with_samsung_note_aspect_for_default_folder():
    pub = _FakePublisher()
    publisher = TriagePublisher(publisher=pub, project_id="p")

    msg_id = publisher.publish(drive_file=_drive_file(), extraction=_extraction())

    assert msg_id == "msg-id-123"
    assert len(pub.captured) == 1
    captured = pub.captured[0]
    assert captured["topic"] == "projects/p/topics/asb-triage-input"
    assert captured["ordering_key"] == "file-1"
    envelope = json.loads(captured["data"].decode("utf-8"))
    assert envelope["source"] == "drive"
    assert envelope["source_url"].endswith("/view")
    assert envelope["source_event_ref"] == "file-1#rev-1"
    assert envelope["sender"] == "owner@example.com"
    assert envelope["subject"] == "meeting.pdf"
    assert envelope["body"] == "# meeting\n- note"
    assert envelope["aspects"] == ["samsung_note"]
    assert "ingested_at" in envelope


def test_publish_appends_hipaa_excluded_aspect_for_hipaa_folder():
    pub = _FakePublisher()
    publisher = TriagePublisher(publisher=pub, project_id="p")

    publisher.publish(
        drive_file=_drive_file(folder=NoteFolder.HIPAA),
        extraction=_extraction(),
    )

    envelope = json.loads(pub.captured[0]["data"].decode("utf-8"))
    assert envelope["aspects"] == ["samsung_note", "hipaa_excluded"]


def test_publish_uses_drive_file_id_as_ordering_key():
    """ADR 0031: ordering_key=drive_file_id keeps redeliveries ordered."""
    pub = _FakePublisher()
    publisher = TriagePublisher(publisher=pub, project_id="p")

    publisher.publish(drive_file=_drive_file(), extraction=_extraction())

    assert pub.captured[0]["ordering_key"] == "file-1"


def test_publish_raises_when_publisher_throws():
    pub = _FakePublisher(raises=RuntimeError("topic not found"))
    publisher = TriagePublisher(publisher=pub, project_id="p")

    with pytest.raises(TriagePublishError) as ei:
        publisher.publish(drive_file=_drive_file(), extraction=_extraction())
    assert "topic not found" in str(ei.value)


def test_publish_raises_when_future_fails():
    @dataclass
    class _BadFuture:
        def result(self, timeout: int | None = None) -> str:
            raise RuntimeError("publish ack timeout")

    pub = _FakePublisher(future=_BadFuture())
    publisher = TriagePublisher(publisher=pub, project_id="p")

    with pytest.raises(TriagePublishError):
        publisher.publish(drive_file=_drive_file(), extraction=_extraction())


def test_topic_path_is_namespaced_to_project():
    pub = _FakePublisher()
    publisher = TriagePublisher(publisher=pub, project_id="agency-brain-demo")
    assert publisher.topic_path == "projects/agency-brain-demo/topics/asb-triage-input"
