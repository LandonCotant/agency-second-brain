"""Tests for ``agency_brain.common.dossier_section_editor``."""

from __future__ import annotations

from typing import Any

import pytest
from agency_brain.common.dossier_section_editor import (
    END_MARKER,
    START_MARKER,
    DossierSectionEditor,
    SectionUpdateResult,
)

# Build a Docs API response shape for `documents.get` that matches what the
# real API returns for our purposes — body.content is a list of paragraph
# elements, each containing textRun runs with startIndex / endIndex / content.


def _doc_with_markers(body_chunks: list[str]) -> dict:
    """Compose a minimal Doc body that includes start + end markers."""
    elements: list[dict] = []
    cursor = 1  # Docs index start at 1
    for chunk in body_chunks:
        end_index = cursor + len(chunk)
        elements.append(
            {
                "endIndex": end_index,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": cursor,
                            "endIndex": end_index,
                            "textRun": {"content": chunk},
                        }
                    ]
                },
            }
        )
        cursor = end_index
    return {"body": {"content": elements}, "revisionId": "rev-abc-123"}


class _FakeDocsService:
    def __init__(self, *, get_doc: dict, batch_response: dict | None = None) -> None:
        self._get_doc = get_doc
        self._batch_response = batch_response or {}
        self.last_batch_requests: list[dict] | None = None
        self.last_batch_body: dict | None = None

    def documents(self) -> _FakeDocsService:
        return self

    def get(self, *, documentId: str) -> _FakeDocsService._GetExec:
        outer = self

        class _GetExec:
            def execute(self_inner) -> dict:
                return outer._get_doc

        return _GetExec()

    def batchUpdate(self, *, documentId: str, body: dict) -> _FakeDocsService._BatchExec:
        outer = self
        outer.last_batch_requests = list(body.get("requests") or [])
        outer.last_batch_body = body

        class _BatchExec:
            def execute(self_inner) -> dict:
                return outer._batch_response

        return _BatchExec()


class _FakeDocsFactory:
    def __init__(self, service: _FakeDocsService) -> None:
        self._service = service

    def build(self) -> Any:
        return self._service


def test_update_existing_markers_replaces_inner_content() -> None:
    doc = _doc_with_markers(
        [
            "Client A dossier\n\n## Related\n",
            f"{START_MARKER}\n",
            "old content goes here\n",
            f"{END_MARKER}\n",
        ]
    )
    svc = _FakeDocsService(get_doc=doc)
    editor = DossierSectionEditor(service_factory=_FakeDocsFactory(svc))
    result = editor.update_related_section(
        doc_id="abc",
        body_lines=["clienta-pi-memo.md", "https://drive/123"],
    )
    assert isinstance(result, SectionUpdateResult)
    assert result.updated is True
    assert result.appended_section is False
    requests = svc.last_batch_requests or []
    # Should issue a deleteContentRange + insertText pair.
    assert any("deleteContentRange" in r for r in requests)
    insert_reqs = [r for r in requests if "insertText" in r]
    assert insert_reqs
    inserted_text = insert_reqs[0]["insertText"]["text"]
    assert "- clienta-pi-memo.md" in inserted_text
    assert "- https://drive/123" in inserted_text
    # writeControl pins the revision the offsets were computed against so a
    # concurrent edit makes Docs reject the stale write instead of corrupting.
    assert svc.last_batch_body["writeControl"] == {"requiredRevisionId": "rev-abc-123"}


def test_missing_markers_appends_section() -> None:
    # Doc with no markers — editor should append at end.
    doc = _doc_with_markers(["Just a regular dossier with no markers yet.\n"])
    svc = _FakeDocsService(get_doc=doc)
    editor = DossierSectionEditor(service_factory=_FakeDocsFactory(svc))
    result = editor.update_related_section(
        doc_id="abc",
        body_lines=["x.md"],
    )
    assert result.updated is True
    assert result.appended_section is True
    requests = svc.last_batch_requests or []
    assert len(requests) == 1
    inserted = requests[0]["insertText"]["text"]
    assert "## Related" in inserted
    assert START_MARKER in inserted
    assert END_MARKER in inserted
    assert "- x.md" in inserted


def test_empty_body_lines_renders_placeholder() -> None:
    doc = _doc_with_markers(
        [
            "Header\n",
            f"{START_MARKER}\n",
            "old\n",
            f"{END_MARKER}\n",
        ]
    )
    svc = _FakeDocsService(get_doc=doc)
    editor = DossierSectionEditor(service_factory=_FakeDocsFactory(svc))
    editor.update_related_section(doc_id="abc", body_lines=[])
    requests = svc.last_batch_requests or []
    insert_reqs = [r for r in requests if "insertText" in r]
    assert "(no related notes yet)" in insert_reqs[0]["insertText"]["text"]


def test_get_failure_raises_dossier_edit_error() -> None:
    class _BoomService(_FakeDocsService):
        def get(self, *, documentId):
            class _GetExec:
                def execute(self_inner):
                    raise RuntimeError("docs api down")

            return _GetExec()

    svc = _BoomService(get_doc={})
    editor = DossierSectionEditor(service_factory=_FakeDocsFactory(svc))
    from agency_brain.common.dossier_section_editor import DossierEditError

    with pytest.raises(DossierEditError):
        editor.update_related_section(doc_id="abc", body_lines=["x"])


def test_doc_id_required() -> None:
    svc = _FakeDocsService(get_doc=_doc_with_markers([]))
    editor = DossierSectionEditor(service_factory=_FakeDocsFactory(svc))
    with pytest.raises(ValueError):
        editor.update_related_section(doc_id="", body_lines=["x"])
