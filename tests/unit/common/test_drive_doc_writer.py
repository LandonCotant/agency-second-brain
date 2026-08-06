"""Tests for ``agency_brain.common.drive_doc_writer``."""

from __future__ import annotations

from typing import Any

import pytest
from agency_brain.common.drive_doc_writer import (
    DocCreationResult,
    DriveDocClient,
    DriveDocWriteError,
    DriveServiceFactory,
)


class _FakeDriveFiles:
    def __init__(
        self, *, response: dict[str, Any] | None = None, raises: Exception | None = None
    ) -> None:
        # Use explicit None check — `response or {default}` collapses empty
        # dicts (which we want for the missing-id test) into the default.
        self._response = (
            {"id": "doc-1", "webViewLink": "https://docs.google.com/document/d/doc-1/edit"}
            if response is None
            else response
        )
        self._raises = raises
        self.last_body: dict[str, Any] | None = None
        self.last_media_mimetype: str | None = None
        self.last_media_was_passed: bool = False

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any,
        fields: str,
        supportsAllDrives: bool = False,
    ) -> _FakeDriveFiles:
        self.last_body = body
        # MediaInMemoryUpload exposes ``mimetype()`` and ``getbytes()`` /
        # ``size()`` across SDK versions. We don't lean on internals;
        # we just record that the upload object was passed in.
        try:
            self.last_media_mimetype = media_body.mimetype()
        except Exception:
            self.last_media_mimetype = None
        self.last_media_was_passed = True
        return self

    def execute(self) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeDriveService:
    def __init__(self, files_impl: _FakeDriveFiles) -> None:
        self._files_impl = files_impl

    def files(self) -> _FakeDriveFiles:
        return self._files_impl


class _FakeFactory(DriveServiceFactory):
    def __init__(self, files_impl: _FakeDriveFiles) -> None:
        self._files_impl = files_impl

    def build(self) -> Any:
        return _FakeDriveService(self._files_impl)


def test_create_doc_returns_id_and_url() -> None:
    files = _FakeDriveFiles()
    client = DriveDocClient(service_factory=_FakeFactory(files))
    result = client.create_doc(
        parent_folder_id="parent-123",
        title="2026-05-07-Thu Reflection",
        body_html="<h1>hi</h1>",
    )
    assert isinstance(result, DocCreationResult)
    assert result.doc_id == "doc-1"
    assert "doc-1" in result.web_view_link
    assert files.last_body == {
        "name": "2026-05-07-Thu Reflection",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["parent-123"],
    }
    assert files.last_media_mimetype == "text/html"
    assert files.last_media_was_passed is True


def test_create_doc_synthesizes_url_if_missing() -> None:
    files = _FakeDriveFiles(response={"id": "doc-99"})
    client = DriveDocClient(service_factory=_FakeFactory(files))
    result = client.create_doc(
        parent_folder_id="parent-123",
        title="Title",
        body_html="<p>body</p>",
    )
    assert result.doc_id == "doc-99"
    assert "doc-99" in result.web_view_link


def test_create_doc_raises_on_empty_parent() -> None:
    client = DriveDocClient(service_factory=_FakeFactory(_FakeDriveFiles()))
    with pytest.raises(ValueError):
        client.create_doc(parent_folder_id="", title="t", body_html="<p>b</p>")


def test_create_doc_raises_on_missing_title() -> None:
    client = DriveDocClient(service_factory=_FakeFactory(_FakeDriveFiles()))
    with pytest.raises(ValueError):
        client.create_doc(parent_folder_id="p", title="   ", body_html="<p>b</p>")


def test_create_doc_wraps_unexpected_errors() -> None:
    files = _FakeDriveFiles(raises=RuntimeError("boom"))
    client = DriveDocClient(service_factory=_FakeFactory(files))
    with pytest.raises(DriveDocWriteError):
        client.create_doc(parent_folder_id="p", title="t", body_html="<p>b</p>")


def test_create_doc_raises_when_response_lacks_id() -> None:
    files = _FakeDriveFiles(response={})
    client = DriveDocClient(service_factory=_FakeFactory(files))
    with pytest.raises(DriveDocWriteError):
        client.create_doc(parent_folder_id="p", title="t", body_html="<p>b</p>")
