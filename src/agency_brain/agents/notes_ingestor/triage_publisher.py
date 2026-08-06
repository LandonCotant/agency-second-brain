"""Publish ingested notes to ``asb-triage-input``.

ADR 0031 §3 introduced this for Samsung Notes; ADR 0037 §6 gated the
publish on ``note_kind`` (only INBOX kind notes publish; AREAS,
RESOURCES, ARCHIVES, GALAXY are reference / synthesis material that
the embedder + Phase 5 Connector surface semantically rather than via
triage).

Each call publishes one Pub/Sub message that the existing Triage bridge
(ADR 0019) will pull on its next 5-min tick. The message envelope mirrors
``triage.bridge._build_triage_input`` so the bridge can deserialize without
modification.

HIPAA folder notes carry the ``hipaa_excluded`` aspect — BaseAgent's
pre-flight short-circuits these into a ``HIPAA_GUARD_TRIPPED`` audit row
without classification (ADR 0006).

Pub/Sub ``ordering_key`` is set to ``drive_file_id`` so redeliveries of
the same note stay ordered with respect to the subscriber.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import (
    DriveFile,
    ExtractionResult,
    NoteFolder,
    is_hipaa_folder,
    should_publish_to_triage,
)

log = logging.getLogger("agency_brain.agents.notes_ingestor.triage_publisher")


class PubSubPublisherClient(Protocol):
    """Matches ``google.cloud.pubsub_v1.PublisherClient.publish`` (sync wait)."""

    def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str = "",
        **attributes: str,
    ) -> Any: ...


class TriagePublishError(RuntimeError):
    pass


class TriagePublisher:
    """Publishes one TriageInput-shaped JSON message per ingested note."""

    def __init__(
        self,
        *,
        publisher: PubSubPublisherClient,
        project_id: str,
        topic_name: str = "asb-triage-input",
        sender_email: str = "owner@example.com",
    ) -> None:
        self._publisher = publisher
        self._topic_path = f"projects/{project_id}/topics/{topic_name}"
        self._sender = sender_email

    @property
    def topic_path(self) -> str:
        return self._topic_path

    def should_publish(self, drive_file: DriveFile) -> bool:
        """ADR 0037 §6 — only INBOX-kind folders publish to triage.

        Reference folders (Areas/Resources/Archives) write the corpus
        row + embedding but never reach the triage rubric.
        """
        return should_publish_to_triage(drive_file.folder)

    def publish(
        self,
        *,
        drive_file: DriveFile,
        extraction: ExtractionResult,
    ) -> str | None:
        """Publish a TriageInput JSON envelope for `drive_file`.

        Returns the Pub/Sub message id. Returns ``None`` (no exception)
        when the folder's ``note_kind`` doesn't trigger triage publish
        per ADR 0037 §6 — this is the explicit "skip publish" path,
        distinct from a publish failure.

        Raises ``TriagePublishError`` on a publisher exception so the
        caller treats it as a per-file failure (the audit row still
        lands and the file is left in the source folder for retry on
        the next tick).
        """
        if not self.should_publish(drive_file):
            log.info(
                "notes_ingestor.publish.skipped_by_kind file_id=%s folder=%s",
                drive_file.file_id,
                drive_file.folder.value,
            )
            return None

        envelope = {
            "source": "drive",
            "source_url": drive_file.web_view_link,
            "source_event_ref": f"{drive_file.file_id}#{drive_file.revision_id}",
            "sender": self._sender,
            "subject": drive_file.name,
            "body": extraction.markdown,
            "ingested_at": datetime.now(UTC).isoformat(),
            "aspects": _aspects_for_folder(drive_file.folder),
        }
        data = json.dumps(envelope).encode("utf-8")
        try:
            future = self._publisher.publish(
                self._topic_path,
                data,
                ordering_key=drive_file.file_id,
            )
        except Exception as exc:
            raise TriagePublishError(f"publish raised: {type(exc).__name__}: {exc}") from exc

        # The real client returns a Future whose .result() is the message
        # id. Tests can pass a fake that returns the id directly; we
        # accept either shape.
        if hasattr(future, "result"):
            try:
                return future.result(timeout=30)
            except Exception as exc:
                raise TriagePublishError(
                    f"publish future failed: {type(exc).__name__}: {exc}"
                ) from exc
        return str(future)


def _aspects_for_folder(folder: NoteFolder) -> list[str]:
    """Map the folder role to the canonical aspect set.

    ``samsung_note`` is always present so downstream readers can filter
    by source — the tag predates the broader Brain layout (ADR 0037)
    but stays in place for back-compat with existing readers and to
    keep the triage envelope shape stable.

    ``hipaa_excluded`` triggers BaseAgent's HIPAA short-circuit
    (ADR 0006); the Triage RE will emit a HIPAA_GUARD_TRIPPED audit
    row and skip classification. That is the desired posture per
    ADR 0031 §3.
    """
    if is_hipaa_folder(folder):
        return ["samsung_note", "hipaa_excluded"]
    return ["samsung_note"]
