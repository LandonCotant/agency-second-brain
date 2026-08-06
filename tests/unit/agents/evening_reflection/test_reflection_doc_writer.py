"""Tests for ``agency_brain.agents.evening_reflection.reflection_doc_writer``."""

from __future__ import annotations

from datetime import date

import pytest
from agency_brain.agents.evening_reflection.reflection_doc_writer import (
    ReflectionDocResult,
    ReflectionDocWriter,
)
from agency_brain.common.drive_doc_writer import (
    DocCreationResult,
    DriveDocClient,
    DriveDocWriteError,
)


class _FakeDriveDocClient(DriveDocClient):
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.last_args: dict[str, object] | None = None

    def create_doc(self, *, parent_folder_id, title, body_html):
        self.last_args = {
            "parent_folder_id": parent_folder_id,
            "title": title,
            "body_html": body_html,
        }
        if self._raises is not None:
            raise self._raises
        return DocCreationResult(
            doc_id="doc-x",
            web_view_link="https://docs.google.com/document/d/doc-x/edit",
        )


def test_write_creates_doc_with_title_convention() -> None:
    fake = _FakeDriveDocClient()
    writer = ReflectionDocWriter(drive_client=fake, parent_folder_id="parent-id")
    result = writer.write(run_date=date(2026, 5, 7), body_html="<h1>hi</h1>")
    assert isinstance(result, ReflectionDocResult)
    assert result.doc_id == "doc-x"
    assert "doc-x" in result.doc_url
    assert fake.last_args == {
        "parent_folder_id": "parent-id",
        "title": "2026-05-07-Thu Reflection",
        "body_html": "<h1>hi</h1>",
    }


def test_title_for_uses_iso_date_and_short_dayname() -> None:
    assert ReflectionDocWriter.title_for(date(2026, 5, 7)) == "2026-05-07-Thu Reflection"
    assert (
        ReflectionDocWriter.title_for(date(2026, 5, 7), suffix="Anchor") == "2026-05-07-Thu Anchor"
    )


def test_constructor_rejects_empty_parent_folder() -> None:
    with pytest.raises(ValueError):
        ReflectionDocWriter(drive_client=_FakeDriveDocClient(), parent_folder_id="")


def test_write_propagates_drive_errors() -> None:
    fake = _FakeDriveDocClient(raises=DriveDocWriteError("boom"))
    writer = ReflectionDocWriter(drive_client=fake, parent_folder_id="p")
    with pytest.raises(DriveDocWriteError):
        writer.write(run_date=date(2026, 5, 7), body_html="<p>b</p>")
