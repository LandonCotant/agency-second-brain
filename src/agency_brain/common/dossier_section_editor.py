"""Idempotent marker-bracketed Doc section update via Docs API ``batchUpdate``.

The Librarian (ADR 0044, Phase D) keeps a managed ``## Related`` section
inside each Areas dossier. The user's hand-authored content elsewhere in
the Doc is sacred — this editor ONLY touches content between two marker
comments:

    <!-- librarian:related:start -->
    ...managed content (replaced on every Librarian tick)...
    <!-- librarian:related:end -->

Idempotent: replaces the section content on every run, never appends.
If the markers are missing, the editor appends the section AT THE END
of the doc (never injected mid-doc) so the next run's edit is bounded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

START_MARKER = "<!-- librarian:related:start -->"
END_MARKER = "<!-- librarian:related:end -->"
SECTION_HEADING = "Related"

log = logging.getLogger("agency_brain.common.dossier_section_editor")


class DocsServiceFactory(Protocol):
    """Builds an authenticated Docs v1 service client."""

    def build(self) -> Any: ...


class DossierEditError(RuntimeError):
    """Docs ``batchUpdate`` / ``documents.get`` call failed."""


@dataclass(frozen=True)
class SectionUpdateResult:
    doc_id: str
    updated: bool
    """True when the managed section was rewritten this run; False when
    the doc had no markers AND no content was passed (no-op)."""
    appended_section: bool
    """True when the markers were absent and we appended a new section
    at the end of the doc."""


class DossierSectionEditor:
    """Updates a marker-bracketed section inside a Google Doc.

    Per ADR 0044 §risk #6 — the editor ONLY rewrites content between
    the start/end markers. The Docs API surface is ``documents.get``
    (to find marker locations) + ``documents.batchUpdate`` (DeleteRange
    + InsertText). When markers are missing, the editor appends the
    section header + markers + body at the doc's end via a single
    InsertText operation.
    """

    def __init__(self, *, service_factory: DocsServiceFactory) -> None:
        self._factory = service_factory
        self._svc: Any | None = None

    def update_related_section(
        self,
        *,
        doc_id: str,
        body_lines: list[str],
    ) -> SectionUpdateResult:
        """Replace (or append) the ``## Related`` section's body.

        ``body_lines`` is a list of plain text lines (no markdown bullets
        — the editor inserts ``- `` prefix per line). Empty list collapses
        to a single ``(no related notes yet)`` placeholder so the doc
        always reads cleanly.
        """
        if not doc_id:
            raise ValueError("doc_id is required")
        svc = self._get_service()
        rendered_body = _render_body(body_lines)

        try:
            doc = svc.documents().get(documentId=doc_id).execute()
        except Exception as exc:
            raise DossierEditError(
                f"documents.get failed for {doc_id}: {type(exc).__name__}: {exc}"
            ) from exc

        # Capture the revision the indices below are computed against. We
        # pass it as writeControl.requiredRevisionId on the batchUpdate so
        # the Docs API rejects the write if anyone (a human editor, another
        # tick) changed the doc in between — otherwise the absolute
        # deleteContentRange/insertText offsets would be stale and could
        # corrupt content outside the managed section.
        revision_id = doc.get("revisionId")

        markers = _find_markers(doc)
        requests: list[dict] = []
        appended = False
        if markers is None:
            # Append the section at the end of the document.
            end_index = _doc_end_index(doc)
            new_section = (
                f"\n## {SECTION_HEADING}\n\n" f"{START_MARKER}\n{rendered_body}\n{END_MARKER}\n"
            )
            requests.append(
                {
                    "insertText": {
                        "location": {"index": end_index},
                        "text": new_section,
                    }
                }
            )
            appended = True
        else:
            # Replace content between (start_end_index, end_start_index).
            start_end_index = markers["start_end_index"]
            end_start_index = markers["end_start_index"]
            if end_start_index > start_end_index:
                # Delete the existing managed content (keeping the
                # markers themselves intact via the index range).
                requests.append(
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": start_end_index,
                                "endIndex": end_start_index,
                            }
                        }
                    }
                )
            requests.append(
                {
                    "insertText": {
                        "location": {"index": start_end_index},
                        "text": "\n" + rendered_body + "\n",
                    }
                }
            )

        if not requests:
            return SectionUpdateResult(doc_id=doc_id, updated=False, appended_section=False)

        body: dict[str, Any] = {"requests": requests}
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}
        try:
            svc.documents().batchUpdate(
                documentId=doc_id,
                body=body,
            ).execute()
        except Exception as exc:
            raise DossierEditError(
                f"documents.batchUpdate failed for {doc_id}: {type(exc).__name__}: {exc}"
            ) from exc

        return SectionUpdateResult(doc_id=doc_id, updated=True, appended_section=appended)

    def _get_service(self) -> Any:
        if self._svc is None:
            self._svc = self._factory.build()
        return self._svc


# --------------------------------------------------------------- helpers


def _render_body(lines: list[str]) -> str:
    cleaned = [ln.strip() for ln in lines if ln and ln.strip()]
    if not cleaned:
        return "(no related notes yet)"
    return "\n".join(f"- {ln}" for ln in cleaned)


def _find_markers(doc: dict) -> dict[str, int] | None:
    """Walk the Docs ``body.content`` array; locate the start/end markers.

    Returns a dict with the index immediately after the start-marker
    text and the index immediately before the end-marker text. Those
    bracket the managed section. Returns None when either marker is
    absent.
    """
    body_content = (doc.get("body") or {}).get("content") or []
    start_end_index: int | None = None
    end_start_index: int | None = None
    for elem in body_content:
        para = elem.get("paragraph")
        if not para:
            continue
        for run in para.get("elements") or []:
            text_run = run.get("textRun")
            if not text_run:
                continue
            content = text_run.get("content") or ""
            start_idx = run.get("startIndex")
            end_idx = run.get("endIndex")
            if START_MARKER in content and start_end_index is None:
                offset = content.index(START_MARKER) + len(START_MARKER)
                start_end_index = (start_idx or 0) + offset
            if END_MARKER in content and end_start_index is None:
                offset = content.index(END_MARKER)
                end_start_index = (start_idx or 0) + offset
    if start_end_index is None or end_start_index is None:
        return None
    return {"start_end_index": start_end_index, "end_start_index": end_start_index}


def _doc_end_index(doc: dict) -> int:
    """The Docs API treats the trailing newline as having endIndex N+1.
    Inserts at ``N`` (one before that) land at the document end.
    """
    body_content = (doc.get("body") or {}).get("content") or []
    end_index = 1
    for elem in body_content:
        ei = elem.get("endIndex")
        if isinstance(ei, int) and ei > end_index:
            end_index = ei
    # Insert just before the trailing newline at end_index.
    return max(end_index - 1, 1)
