"""Tests for ``LibrarianDriveLister``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agency_brain.agents.librarian.drive_lister import (
    LibrarianDriveLister,
    LibrarianListError,
)


class _FakeDriveListClient:
    def __init__(self, *, by_folder: dict[str, list[dict]] | None = None) -> None:
        self._by_folder = by_folder or {}
        self.calls: list[str] = []

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]:
        self.calls.append(folder_id)
        return list(self._by_folder.get(folder_id, []))


def _row(file_id: str, name: str, *, modified: str, mime: str = "application/pdf") -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": modified,
        "parents": [],
    }


def test_list_returns_drop_files() -> None:
    drive = _FakeDriveListClient(
        by_folder={
            "drop-folder": [
                _row("f1", "ClientC brief.pdf", modified="2026-05-07T10:00:00Z"),
                _row("f2", "ad-hoc memo.md", modified="2026-05-07T11:00:00Z", mime="text/markdown"),
            ]
        }
    )
    lister = LibrarianDriveLister(drive_client=drive, max_per_tick=10)
    files = lister.list([("drop-folder", "drop")])
    assert [f.file_id for f in files] == ["f1", "f2"]
    assert all(f.parent_folder_role == "drop" for f in files)


def test_list_caps_at_max_per_tick() -> None:
    rows = [_row(f"f{i}", f"name-{i}", modified="2026-05-07T10:00:00Z") for i in range(10)]
    drive = _FakeDriveListClient(by_folder={"drop": rows})
    lister = LibrarianDriveLister(drive_client=drive, max_per_tick=3)
    files = lister.list([("drop", "drop")])
    assert len(files) == 3


def test_list_excludes_subfolders() -> None:
    drive = _FakeDriveListClient(
        by_folder={
            "drop": [
                _row("f1", "real-file.pdf", modified="2026-05-07T10:00:00Z"),
                _row(
                    "subfolder",
                    "nested",
                    modified="2026-05-07T10:00:00Z",
                    mime="application/vnd.google-apps.folder",
                ),
            ]
        }
    )
    lister = LibrarianDriveLister(drive_client=drive, max_per_tick=10)
    files = lister.list([("drop", "drop")])
    assert [f.file_id for f in files] == ["f1"]


def test_list_quicknotes_skips_fresh_files() -> None:
    fresh = _row("f-fresh", "fresh.md", modified="2026-05-07T10:00:00Z", mime="text/markdown")
    old = _row("f-old", "old.md", modified="2026-04-01T10:00:00Z", mime="text/markdown")
    drive = _FakeDriveListClient(by_folder={"qn": [fresh, old]})
    lister = LibrarianDriveLister(
        drive_client=drive,
        max_per_tick=10,
        quicknotes_age_days=14,
        now=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
    )
    files = lister.list([("qn", "quicknotes")])
    assert [f.file_id for f in files] == ["f-old"]


def test_list_refuses_hipaa_role() -> None:
    drive = _FakeDriveListClient()
    lister = LibrarianDriveLister(drive_client=drive)
    with pytest.raises(LibrarianListError):
        lister.list([("hipaa-folder-id", "hipaa")])


def test_list_skips_empty_folder_id() -> None:
    drive = _FakeDriveListClient(
        by_folder={"drop": [_row("f1", "x.pdf", modified="2026-05-07T10:00:00Z")]}
    )
    lister = LibrarianDriveLister(drive_client=drive)
    files = lister.list([("", "drop"), ("drop", "drop")])
    assert [f.file_id for f in files] == ["f1"]


def test_skip_file_ids_filters_already_processed_files() -> None:
    drive = _FakeDriveListClient(
        by_folder={
            "drop": [
                _row(
                    "f-old",
                    "already-moved.md",
                    modified="2026-05-07T10:00:00Z",
                    mime="text/markdown",
                ),
                _row("f-new", "fresh.md", modified="2026-05-07T11:00:00Z", mime="text/markdown"),
            ]
        }
    )
    lister = LibrarianDriveLister(
        drive_client=drive,
        max_per_tick=10,
        skip_file_ids={"f-old"},
    )
    files = lister.list([("drop", "drop")])
    assert [f.file_id for f in files] == ["f-new"]


def test_skip_file_ids_default_empty_does_not_skip() -> None:
    drive = _FakeDriveListClient(
        by_folder={"drop": [_row("f1", "x.md", modified="2026-05-07T10:00:00Z")]}
    )
    lister = LibrarianDriveLister(drive_client=drive, max_per_tick=10)
    files = lister.list([("drop", "drop")])
    assert [f.file_id for f in files] == ["f1"]


def test_list_continues_when_one_folder_errors() -> None:
    class _Boom(_FakeDriveListClient):
        def list_files(self, *, folder_id, page_size, fields):
            if folder_id == "boom":
                raise RuntimeError("transient")
            return super().list_files(folder_id=folder_id, page_size=page_size, fields=fields)

    drive = _Boom(by_folder={"drop": [_row("f1", "x.pdf", modified="2026-05-07T10:00:00Z")]})
    lister = LibrarianDriveLister(drive_client=drive)
    files = lister.list([("boom", "drop"), ("drop", "drop")])
    # Still returns the second folder's contents — the bad folder is logged + skipped.
    assert [f.file_id for f in files] == ["f1"]
