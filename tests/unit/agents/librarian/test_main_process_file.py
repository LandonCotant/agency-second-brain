"""Tests for the per-file pipeline ordering in librarian main.

The archive-after-ingest ordering is load-bearing: on the copy-fallback
path the original must NOT be archived into source/processed/ until the
corpus write is confirmed. Archiving first turned an ingest failure into
silent content loss (original hidden in processed/, no corpus row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agency_brain.agents.librarian.main import _process_file
from agency_brain.agents.librarian.models import AreaFolder, DropFile
from agency_brain.agents.librarian.mover import MoveResult


def _drop_file() -> DropFile:
    return DropFile(
        file_id="orig-1",
        name="scan.pdf",
        mime_type="application/pdf",
        parent_folder_id="drop-folder",
        parent_folder_role="drop",
        modified_time=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
    )


class _FakeMover:
    """Simulates the cross-Drive copy-fallback: move() returns a NEW file_id."""

    def __init__(self) -> None:
        self.archive_calls: list[str] = []
        self.event_order: list[str] = []

    def move(self, *, file_id, from_folder_id, to_folder_id, to_folder_path):
        self.event_order.append("move")
        return MoveResult(
            file_id="copy-2",  # != original → copy-fallback path
            from_folder_id=from_folder_id,
            to_folder_id=to_folder_id,
            to_folder_path=to_folder_path,
        )

    def archive_processed_original(self, *, file_id, source_folder_id):
        self.event_order.append("archive")
        self.archive_calls.append(file_id)
        return True

    def rename_file(self, *, file_id, new_name):
        return True

    def get_or_create_uncategorized(self):
        return "uncat-folder"


class _FakeIngestor:
    def __init__(self, *, fail: bool, order_sink: list[str]) -> None:
        self._fail = fail
        self._order = order_sink

    def ingest(self, *, drop_file, dest_folder, markdown):
        self._order.append("ingest")
        if self._fail:
            raise RuntimeError("BQ write timed out")
        return SimpleNamespace(
            note_id="note-1", written=True, deduped=False, embedded=True, error=None
        )


class _FakeLinker:
    def link_for_drive_file(self, *, drive_file_id, dossier_doc_id):
        from agency_brain.agents.librarian.models import LinkerOutcome

        return LinkerOutcome()


class _RaisingDriveClient:
    """Extraction + dossier lookup are best-effort try/except paths —
    raising here exercises their fallbacks without faking Drive."""

    def __getattr__(self, name):
        raise RuntimeError("drive unavailable in unit test")


def _run(*, ingest_fails: bool) -> tuple[_FakeMover, object]:
    mover = _FakeMover()
    ingestor = _FakeIngestor(fail=ingest_fails, order_sink=mover.event_order)
    dest = AreaFolder(id="dest-1", name="clienta-pi", path="clients/clienta-pi")
    outcome = _process_file(
        drive_file=_drop_file(),
        drive_client=_RaisingDriveClient(),
        extractor=None,
        classifier=SimpleNamespace(
            classify=lambda **kw: SimpleNamespace(
                dest_folder_path="clients/clienta-pi",
                confidence=0.95,
                suggested_description=None,
            )
        ),
        candidates=[dest],
        mover=mover,
        linker=_FakeLinker(),
        ingestor=ingestor,
        confidence_threshold=0.7,
        dossier_filename="",
        areas_index=None,
    )
    return mover, outcome


def test_copy_fallback_archives_only_after_successful_ingest() -> None:
    mover, outcome = _run(ingest_fails=False)
    assert mover.event_order == ["move", "ingest", "archive"]
    assert mover.archive_calls == ["orig-1"]
    assert outcome.ingest.written is True


def test_copy_fallback_keeps_original_when_ingest_fails() -> None:
    """Ingest failure must leave the original in Drop for the next tick —
    archiving it would hide the file with no corpus row anywhere."""
    mover, outcome = _run(ingest_fails=True)
    assert "archive" not in mover.event_order
    assert mover.archive_calls == []
    assert outcome.ingest.error is not None
    assert outcome.moved is True  # the Drive copy itself did land
