"""Tests for ``people_sync.drive_writer`` — fake Drive service.

The real service is googleapiclient with chained method calls
(``svc.files().create().execute()``). The fake mimics that shape so
the writer under test stays SDK-agnostic.
"""

from __future__ import annotations

from typing import Any

from agency_brain.agents.people_sync.drive_writer import (
    MARKDOWN_MIME,
    PeopleDriveWriter,
)

# --------------------------------------------------------------- fake Drive service


class _FakeMediaUpload:
    """Stand-in for googleapiclient's MediaInMemoryUpload — just records bytes."""

    def __init__(self, body: bytes, mimetype: str) -> None:
        self.body = body
        self.mimetype = mimetype


class _Request:
    """A pending API request whose .execute() returns the canned result."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _FakeFiles:
    """The .files() endpoint."""

    def __init__(self, store: dict[str, dict]) -> None:
        # store: file_id → {"name", "content", "parents"}
        self._store = store
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.get_media_calls: list[str] = []

    def list(
        self,
        *,
        q: str,
        pageSize: int,
        fields: str,
        supportsAllDrives: bool,
        includeItemsFromAllDrives: bool,
        pageToken: str | None = None,
    ):
        self.list_calls.append({"q": q, "pageToken": pageToken})
        # Naive: return everything in the fake store (real server filters by q),
        # one item per page so the pagination loop is genuinely exercised.
        items = [
            {"id": fid, "name": f["name"], "mimeType": MARKDOWN_MIME}
            for fid, f in self._store.items()
        ]
        start = int(pageToken) if pageToken else 0
        if start >= len(items):
            return _Request({"files": []})
        page = {"files": [items[start]]}
        if start + 1 < len(items):
            page["nextPageToken"] = str(start + 1)
        return _Request(page)

    def get_media(self, *, fileId: str, supportsAllDrives: bool):
        self.get_media_calls.append(fileId)
        return _DownloadRequest(self._store[fileId]["content"])

    def create(
        self, *, body: dict, media_body: _FakeMediaUpload, fields: str, supportsAllDrives: bool
    ):
        self.create_calls.append({"body": body, "content": media_body.body.decode("utf-8")})
        new_id = f"fake-id-{len(self._store) + 1}"
        self._store[new_id] = {
            "name": body["name"],
            "content": media_body.body.decode("utf-8"),
            "parents": body.get("parents", []),
        }
        return _Request({"id": new_id})

    def update(self, *, fileId: str, media_body: _FakeMediaUpload, supportsAllDrives: bool):
        self.update_calls.append(
            {
                "fileId": fileId,
                "content": media_body.body.decode("utf-8"),
            }
        )
        self._store[fileId]["content"] = media_body.body.decode("utf-8")
        return _Request({"id": fileId})


class _DownloadRequest:
    """Fake the MediaIoBaseDownload chunk-stream — returns full content on first chunk."""

    def __init__(self, content: str) -> None:
        self._content = content.encode("utf-8")

    def execute(self):  # not used; the writer goes through MediaIoBaseDownload
        return self._content


class _FakeDriveService:
    def __init__(self, store: dict[str, dict] | None = None) -> None:
        self._store = store if store is not None else {}
        self._files = _FakeFiles(self._store)

    def files(self):
        return self._files


class _FakeFactory:
    def __init__(self, *, store: dict[str, dict] | None = None) -> None:
        self.service = _FakeDriveService(store)

    def build(self):
        return self.service


# Monkeypatch the MediaInMemoryUpload + MediaIoBaseDownload imports inside drive_writer
# so the tests don't need the real googleapiclient dependencies.


import pytest


@pytest.fixture(autouse=True)
def _patch_googleapi(monkeypatch):
    """Replace MediaInMemoryUpload + MediaIoBaseDownload with fakes.

    The drive_writer imports these lazily inside the method. Patch them
    in the googleapiclient.http namespace so the lazy import returns
    our fakes.
    """
    import sys
    import types

    fake_http = types.ModuleType("googleapiclient.http")

    class _FakeMediaIoBaseDownload:
        def __init__(self, buf, request):
            self._buf = buf
            self._req = request

        def next_chunk(self):
            self._buf.write(self._req._content)
            return (None, True)

    fake_http.MediaInMemoryUpload = _FakeMediaUpload
    fake_http.MediaIoBaseDownload = _FakeMediaIoBaseDownload

    fake_errors = types.ModuleType("googleapiclient.errors")

    class _FakeHttpError(Exception):
        pass

    fake_errors.HttpError = _FakeHttpError

    fake_root = types.ModuleType("googleapiclient")
    fake_root.http = fake_http
    fake_root.errors = fake_errors

    monkeypatch.setitem(sys.modules, "googleapiclient", fake_root)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", fake_http)
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", fake_errors)
    yield


# --------------------------------------------------------------- tests


def _seed_file(content: str) -> dict[str, dict]:
    return {
        "existing-id-1": {"name": "Client A.md", "content": content, "parents": ["clients-folder"]}
    }


def test_list_existing_follows_pagination_across_pages() -> None:
    """The fake returns one file per page; list_existing must walk
    nextPageToken so files past page 1 aren't treated as missing (which
    would make upsert create duplicates)."""
    store = {
        f"id-{i}": {
            "name": f"Client {i}.md",
            "content": f"---\ntype: account\nairtable_id: rec{i}\n---\nbody\n",
            "parents": ["clients-folder"],
        }
        for i in range(3)
    }
    factory = _FakeFactory(store=store)
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="clients-folder")
    existing = writer.list_existing()
    assert {"rec0", "rec1", "rec2"} <= set(existing.keys())
    # Three single-item pages → three list calls, the last two with a token.
    tokens = [c["pageToken"] for c in factory.service._files.list_calls]
    assert tokens == [None, "1", "2"]


def test_upsert_new_creates_file_with_skeleton() -> None:
    factory = _FakeFactory()
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="clients-folder")
    fm = {"type": "account", "airtable_id": "recAAA", "name": "Client A", "status": "active"}
    body = "# Client A\n\n## Who they are\n<empty>\n"
    outcome = writer.upsert(
        filename="Client A.md",
        airtable_id="recAAA",
        new_frontmatter=fm,
        body_skeleton=body,
    )
    assert outcome.created is True
    assert outcome.frontmatter_updated is False
    assert outcome.error is None
    create = factory.service._files.create_calls
    assert len(create) == 1
    assert create[0]["body"]["name"] == "Client A.md"
    assert "airtable_id: recAAA" in create[0]["content"]
    assert "# Client A" in create[0]["content"]


def test_upsert_unchanged_is_skip() -> None:
    existing_content = """---
type: account
airtable_id: recAAA
name: Client A
status: active
synced_at: 2026-05-17T15:00:00Z
---

# Client A
"""
    factory = _FakeFactory(store=_seed_file(existing_content))
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="clients-folder")
    # Same frontmatter except synced_at — should be a no-op
    fm = {
        "type": "account",
        "airtable_id": "recAAA",
        "name": "Client A",
        "status": "active",
        "synced_at": "2026-05-18T15:00:00Z",
    }
    outcome = writer.upsert(
        filename="Client A.md",
        airtable_id="recAAA",
        new_frontmatter=fm,
        body_skeleton="(unused)",
    )
    assert outcome.skipped_unchanged is True
    assert outcome.frontmatter_updated is False
    assert outcome.created is False
    assert factory.service._files.update_calls == []
    assert factory.service._files.create_calls == []


def test_upsert_relevant_change_rewrites_keeping_body() -> None:
    existing_content = """---
type: account
airtable_id: recAAA
name: Client A
status: active
synced_at: 2026-05-17T15:00:00Z
---

# Client A

## Who they are
User-written prose about Client A that MUST be preserved.

## Active engagements
<!-- AUTO: populated by asb-people-sync -->
"""
    factory = _FakeFactory(store=_seed_file(existing_content))
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="clients-folder")
    # Status changed → should trigger update
    fm = {
        "type": "account",
        "airtable_id": "recAAA",
        "name": "Client A",
        "status": "churned",
        "synced_at": "2026-05-18T15:00:00Z",
    }
    outcome = writer.upsert(
        filename="Client A.md",
        airtable_id="recAAA",
        new_frontmatter=fm,
        body_skeleton="(should not be used — body preserved)",
    )
    assert outcome.frontmatter_updated is True
    assert outcome.created is False
    assert outcome.skipped_unchanged is False
    update = factory.service._files.update_calls
    assert len(update) == 1
    new_content = update[0]["content"]
    assert "status: churned" in new_content
    # CRITICAL: user prose preserved
    assert "User-written prose about Client A that MUST be preserved." in new_content
    # AUTO marker also preserved (Phase 2 enricher handles updating it)
    assert "<!-- AUTO: populated by asb-people-sync -->" in new_content


def test_upsert_filename_collision_disambiguator() -> None:
    """Two different airtable_ids with same display name → second gets a suffix."""
    factory = _FakeFactory()
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="people-folder")

    # First contact lands cleanly
    writer.upsert(
        filename="Sam Q.md",
        airtable_id="recABC123",
        new_frontmatter={"type": "person", "airtable_id": "recABC123", "name": "Sam Q"},
        body_skeleton="# Sam Q\n",
    )
    # Second contact, same name but different airtable_id → collision
    outcome = writer.upsert(
        filename="Sam Q.md",
        airtable_id="recXYZ789",
        new_frontmatter={"type": "person", "airtable_id": "recXYZ789", "name": "Sam Q"},
        body_skeleton="# Sam Q\n",
    )
    assert outcome.created is True
    # Filename should have the disambiguator
    assert "collision-" in outcome.filename
    assert outcome.filename.endswith(".md")


def test_mark_archived_flips_status_preserves_body() -> None:
    existing_content = """---
type: person
airtable_id: recDEL
name: Gone Contact
warmth: cold
synced_at: 2026-05-17T15:00:00Z
---

# Gone Contact

## Who they are
They were great.
"""
    factory = _FakeFactory(store=_seed_file(existing_content))
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="people-folder")

    outcome = writer.mark_archived(airtable_id="recDEL")
    assert outcome is not None
    assert outcome.frontmatter_updated is True
    update = factory.service._files.update_calls[0]
    assert "status: archived" in update["content"]
    assert "archived_at:" in update["content"]
    assert "archived_reason:" in update["content"]
    assert "They were great." in update["content"]
    # Original keys preserved
    assert "airtable_id: recDEL" in update["content"]
    assert "warmth: cold" in update["content"]


def test_mark_archived_no_existing_file_returns_none() -> None:
    factory = _FakeFactory()
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="people-folder")
    outcome = writer.mark_archived(airtable_id="recDoesNotExist")
    assert outcome is None


def test_mark_archived_already_archived_is_noop() -> None:
    already = """---
type: person
airtable_id: recDEL
status: archived
archived_at: 2026-05-10T00:00:00Z
---

# Was Archived Last Week
"""
    factory = _FakeFactory(store=_seed_file(already))
    writer = PeopleDriveWriter(service_factory=factory, parent_folder_id="people-folder")
    outcome = writer.mark_archived(airtable_id="recDEL")
    assert outcome is not None
    assert outcome.skipped_unchanged is True
    assert factory.service._files.update_calls == []
