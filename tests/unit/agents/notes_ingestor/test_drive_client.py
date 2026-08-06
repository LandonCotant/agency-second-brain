"""Unit tests for ``NotesDriveClient.list_new_pdfs``.

Regression coverage for the 2026-05-04 first-weekly-run bug: Samsung
Notes' "Save to Drive (PDF)" sometimes uploads with a non-standard
``mimeType`` that survives Drive's auto-detection, so a strict
``mimeType = 'application/pdf'`` filter silently drops the file.
The fix OR's mimeType against a ``name contains '.pdf'`` clause; the
name suffix is the load-bearing check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.notes_ingestor.drive_client import (
    FolderConfig,
    NotesDriveClient,
)
from agency_brain.agents.notes_ingestor.models import NoteFolder

# ---------------------------------------------------------------------------
# Fakes — minimal stand-in for the googleapiclient Drive service
# ---------------------------------------------------------------------------


@dataclass
class _FakeListRequest:
    response: dict
    captured_kwargs: dict

    def execute(self) -> dict:
        return self.response


@dataclass
class _FakeFiles:
    """Captures the kwargs each ``list()`` call received."""

    pages: list[dict] = field(default_factory=list)
    list_calls: list[dict] = field(default_factory=list)

    def list(self, **kwargs: Any) -> _FakeListRequest:
        self.list_calls.append(kwargs)
        if not self.pages:
            return _FakeListRequest(response={"files": []}, captured_kwargs=kwargs)
        return _FakeListRequest(response=self.pages.pop(0), captured_kwargs=kwargs)


@dataclass
class _FakeService:
    files_obj: _FakeFiles = field(default_factory=_FakeFiles)

    def files(self) -> _FakeFiles:
        return self.files_obj


@dataclass
class _FakeFactory:
    service: _FakeService

    def build(self) -> _FakeService:
        return self.service


def _client(pages: list[dict]) -> tuple[NotesDriveClient, _FakeFiles]:
    files_obj = _FakeFiles(pages=list(pages))
    service = _FakeService(files_obj=files_obj)
    factory = _FakeFactory(service=service)
    return NotesDriveClient(service_factory=factory), files_obj


_FOLDER = FolderConfig(folder_id="folder-A", role=NoteFolder.DEFAULT)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_query_includes_pdf_name_suffix_or_clause() -> None:
    """The Drive `q=` clause must accept either mimeType or `.pdf` suffix.

    Regression: the prior `mimeType = 'application/pdf'`-only filter
    silently dropped Samsung Notes PDFs uploaded with non-standard
    mimeType (observed 2026-05-04 first weekly run).
    """
    client, files_obj = _client(pages=[{"files": []}])

    list(client.list_new_pdfs(folder=_FOLDER, since=None))

    assert len(files_obj.list_calls) == 1
    q = files_obj.list_calls[0]["q"]
    assert "mimeType = 'application/pdf'" in q
    assert "name contains '.pdf'" in q
    assert "(mimeType = 'application/pdf' or name contains '.pdf')" in q


def test_yields_file_with_non_pdf_mimetype_but_pdf_name() -> None:
    """A file Samsung uploaded with the wrong mimeType still yields.

    The fake Drive returns the file because its `q=` filter would have
    matched on the name-suffix branch. This asserts the client trusts
    Drive's filtering and doesn't add a redundant client-side mimeType
    gate that would re-introduce the bug.
    """
    samsung_file = {
        "id": "file-samsung-1",
        "name": "2026-05-04_meeting.pdf",
        "mimeType": "application/octet-stream",  # the bug-bait mimeType
        "modifiedTime": "2026-05-04T13:00:00Z",
        "webViewLink": "https://drive.google.com/file/d/file-samsung-1/view",
        "headRevisionId": "rev-1",
    }
    client, _ = _client(pages=[{"files": [samsung_file]}])

    out = list(client.list_new_pdfs(folder=_FOLDER, since=None))

    assert len(out) == 1
    assert out[0].file_id == "file-samsung-1"
    assert out[0].name == "2026-05-04_meeting.pdf"
    assert out[0].revision_id == "rev-1"


def test_response_fields_request_includes_mimetype() -> None:
    """`fields=` must request `mimeType` so the diagnostic log can show it.

    The 2026-05-04 incident was diagnosable only because the per-page
    log line surfaced `(name, mimeType)` tuples. Regression-guard
    against a future tidy-up that drops the field from the request.
    """
    client, files_obj = _client(pages=[{"files": []}])

    list(client.list_new_pdfs(folder=_FOLDER, since=None))

    fields = files_obj.list_calls[0]["fields"]
    assert "mimeType" in fields
    assert "headRevisionId" in fields


def test_since_watermark_appends_modified_time_clause() -> None:
    """When `since` is set, the query gates by `modifiedTime > since`.

    The client renders the timestamp via ``astimezone().isoformat()``,
    so the exact wall-clock string depends on the runner's local
    timezone. Assert the clause is present + parseable, not the
    literal date.
    """
    client, files_obj = _client(pages=[{"files": []}])
    since = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)

    list(client.list_new_pdfs(folder=_FOLDER, since=since))

    q = files_obj.list_calls[0]["q"]
    assert "modifiedTime >" in q
    # Same UTC instant; local wall-clock depends on runner TZ (PT→T17, ET→T20, …)
    assert ("2026-05-01" in q) or ("2026-04-30" in q)


def test_paginates_until_nextpagetoken_absent() -> None:
    """Multi-page responses keep paging until `nextPageToken` is empty."""
    page1 = {
        "files": [
            {
                "id": "f1",
                "name": "a.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-05-04T10:00:00Z",
                "webViewLink": "",
                "headRevisionId": "r1",
            }
        ],
        "nextPageToken": "tok-2",
    }
    page2 = {
        "files": [
            {
                "id": "f2",
                "name": "b.pdf",
                "mimeType": "application/octet-stream",
                "modifiedTime": "2026-05-04T11:00:00Z",
                "webViewLink": "",
                "headRevisionId": "r2",
            }
        ]
    }
    client, files_obj = _client(pages=[page1, page2])

    out = list(client.list_new_pdfs(folder=_FOLDER, since=None))

    assert [f.file_id for f in out] == ["f1", "f2"]
    assert len(files_obj.list_calls) == 2
    assert files_obj.list_calls[1]["pageToken"] == "tok-2"


def test_falls_back_to_modifiedtime_when_revision_missing() -> None:
    """If Drive omits `headRevisionId`, the dedup key uses modifiedTime.

    Documented fallback in `drive_client.py:130-135`. Guards against an
    accidental empty-string dedup key, which would let a re-edited file
    re-ingest forever.
    """
    file_no_rev = {
        "id": "file-no-rev",
        "name": "x.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-05-04T12:34:56Z",
        "webViewLink": "",
        # headRevisionId intentionally absent
    }
    client, _ = _client(pages=[{"files": [file_no_rev]}])

    out = list(client.list_new_pdfs(folder=_FOLDER, since=None))

    assert len(out) == 1
    assert out[0].revision_id != ""
    assert "2026-05-04" in out[0].revision_id


# ---------------------------------------------------------------------------
# ADR 0048 — Shared Drive support + recursive walker + subfolder lister
# ---------------------------------------------------------------------------


def test_list_includes_shared_drive_flags() -> None:
    """ADR 0048 — list calls must pass ``supportsAllDrives`` +
    ``includeItemsFromAllDrives`` so Shared Drive items (the Agency
    Solutions root) return alongside My Drive items.
    """
    client, files_obj = _client(pages=[{"files": []}])
    list(client.list_new_pdfs(folder=_FOLDER, since=None))

    call = files_obj.list_calls[0]
    assert call["supportsAllDrives"] is True
    assert call["includeItemsFromAllDrives"] is True


def test_recursive_walker_descends_subfolders() -> None:
    """ADR 0048 §2 — recursive=True yields files from every descendant."""
    # Setup: root has 2 child folders; each child has 1 file. The fake
    # serves responses based on insertion order, so we line them up:
    #   1) list_immediate_subfolders calls _list_child_folder_ids
    #      → page asking for folders under "sales-root"
    #   2) for each subfolder returned, _list_in_one_parent runs once
    #      for sales-root itself + once for each descendant folder
    #
    # The walker yields parent_ids in BFS order: root → children.
    # That means: 3 list calls (one per parent), each potentially
    # returning files.
    #
    # We give responses in order: child-folders-of-root, files-in-root
    # (empty), files-in-child-1, files-in-child-2.
    file_in_child1 = {
        "id": "f-1",
        "name": "deal.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-05-10T10:00:00Z",
        "webViewLink": "",
        "headRevisionId": "r1",
    }
    file_in_child2 = {
        "id": "f-2",
        "name": "pitch.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-05-10T11:00:00Z",
        "webViewLink": "",
        "headRevisionId": "r2",
    }
    client, files_obj = _client(
        pages=[
            # 1) child-folders of sales-root
            {"files": [{"id": "child-1"}, {"id": "child-2"}]},
            # 2) files directly in sales-root (none)
            {"files": []},
            # 3) child-folders of child-1 (none)
            {"files": []},
            # 4) files in child-1
            {"files": [file_in_child1]},
            # 5) child-folders of child-2 (none)
            {"files": []},
            # 6) files in child-2
            {"files": [file_in_child2]},
        ]
    )

    folder = FolderConfig(
        folder_id="sales-root",
        role=NoteFolder.SOLUTIONS_INTERNAL,
        recursive=True,
    )

    out = list(client.list_new_files(folder=folder, since=None))

    file_ids = {f.file_id for f in out}
    assert file_ids == {"f-1", "f-2"}


def test_list_immediate_subfolders_returns_id_and_name() -> None:
    """``list_immediate_subfolders`` is the discovery primitive for the
    Solutions client sweep — must return both id + name so callers can
    apply the HIPAA filter on names before walking files."""
    client, files_obj = _client(
        pages=[
            {
                "files": [
                    {"id": "client-1-id", "name": "06_CLIENT_A"},
                    {"id": "client-2-id", "name": "05_CLIENT_C_STUDIO"},
                ]
            }
        ]
    )

    out = client.list_immediate_subfolders("clients-root")

    assert out == [
        {"id": "client-1-id", "name": "06_CLIENT_A"},
        {"id": "client-2-id", "name": "05_CLIENT_C_STUDIO"},
    ]
    # Verify the q filters on folder mimeType
    q = files_obj.list_calls[0]["q"]
    assert "mimeType = 'application/vnd.google-apps.folder'" in q
    assert "'clients-root' in parents" in q
