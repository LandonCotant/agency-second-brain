"""Google Doc creator for ADR 0044 (Drive write via folder share).

Creates a Google Doc inside a parent Drive folder by uploading an HTML
body and letting Drive auto-convert to a Google Doc on the way in:

    drive.files.create(
        body={"name": title, "mimeType": "application/vnd.google-apps.document",
              "parents": [parent_folder_id]},
        media_body=MediaIoBaseUpload(BytesIO(html_bytes), mimetype="text/html"),
    )

This is one API call and produces full-fidelity headings/bold/lists/etc.
without any Docs API ``batchUpdate`` plumbing — see ADR 0044 §6 for the
rejection of plain-text and markdown-parsing alternatives.

The SA accesses Drive under its **own identity** (no DWD impersonation;
ADR 0044 §1). The user manually shares the parent folder with the SA's
email as Editor; the SA's ADC credentials are built with the
``https://www.googleapis.com/auth/drive`` scope. Both pieces are
load-bearing — without the scope, Drive returns 403; without the share,
Drive returns 404.

Tests inject a fake ``DriveServiceFactory`` so unit tests stay
SDK-agnostic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

log = logging.getLogger("agency_brain.common.drive_doc_writer")


class DriveDocWriteError(RuntimeError):
    """Drive write failed (4xx, 5xx, network, etc.)."""


class DriveServiceFactory(Protocol):
    """Builds an authenticated Drive v3 service client.

    Production: ``ADCDriveServiceFactory`` (this module). Tests: a fake
    that returns a stub mimicking the ``files().create()`` chain.
    """

    def build(self) -> Any: ...


@dataclass(frozen=True)
class DocCreationResult:
    """What ``DriveDocClient.create_doc`` returns to its caller."""

    doc_id: str
    web_view_link: str
    """Drive ``webViewLink`` — what the user clicks to open the Doc."""


class DriveDocClient:
    """Creates Google Docs in a parent Drive folder via HTML auto-convert.

    Per ADR 0044 §6 — HTML upload with ``mimeType=
    application/vnd.google-apps.document`` is the chosen rendering path.
    The body is composed by the caller (see
    ``agents/evening_reflection/composer.render_reflection_doc_body_html``)
    and handed to ``create_doc`` as a UTF-8 string.

    All Drive operations go through ``service_factory.build()`` so unit
    tests can inject a fake without touching ``googleapiclient``.
    """

    def __init__(self, *, service_factory: DriveServiceFactory) -> None:
        self._factory = service_factory
        self._svc: Any | None = None

    def create_doc(
        self,
        *,
        parent_folder_id: str,
        title: str,
        body_html: str,
    ) -> DocCreationResult:
        """Create a Google Doc inside ``parent_folder_id`` with the given title and HTML body.

        Returns the new doc's id + ``webViewLink``. Raises
        ``DriveDocWriteError`` on transport / API failures so callers can
        emit a single audit row and continue (the daily ritual must
        ship — even a degraded Reflection Doc is preferable to no
        artifact).
        """
        if not parent_folder_id:
            raise ValueError("parent_folder_id must be non-empty")
        if not title or not title.strip():
            raise ValueError("title must be non-empty")

        svc = self._get_service()
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaInMemoryUpload

        # ``MediaInMemoryUpload`` keeps the bytes in RAM — fine for the
        # daily Reflection Doc (~3 KB HTML); avoids a tempfile dance.
        body_bytes = body_html.encode("utf-8")
        media = MediaInMemoryUpload(body_bytes, mimetype="text/html")

        try:
            response = (
                svc.files()
                .create(
                    body={
                        "name": title,
                        "mimeType": GOOGLE_DOC_MIME,
                        "parents": [parent_folder_id],
                    },
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveDocWriteError(
                f"Drive files.create failed: HTTP {exc.resp.status}: {exc}"
            ) from exc
        except Exception as exc:
            raise DriveDocWriteError(
                f"Drive files.create raised {type(exc).__name__}: {exc}"
            ) from exc

        doc_id = response.get("id")
        web_view_link = response.get("webViewLink") or _doc_url_from_id(doc_id)
        if not doc_id:
            raise DriveDocWriteError(f"Drive files.create returned no id: {response!r}")
        return DocCreationResult(doc_id=doc_id, web_view_link=web_view_link)

    def _get_service(self) -> Any:
        if self._svc is None:
            self._svc = self._factory.build()
        return self._svc


# --------------------------------------------------------------- production


class ADCDriveServiceFactory:
    """Builds a Drive v3 service client via Application Default Credentials.

    Mirrors ``notes_ingestor.drive_client.ADCDriveServiceFactory`` exactly
    so the read + write surfaces share the same auth pattern. ADR 0044 §1.
    """

    def __init__(self, *, scope: str = DRIVE_SCOPE) -> None:
        self._scope = scope

    def build(self) -> Any:  # pragma: no cover — exercised in integ
        from google.auth import default
        from googleapiclient.discovery import build

        creds, _ = default(scopes=[self._scope])
        return build("drive", "v3", credentials=creds, cache_discovery=False)


def _doc_url_from_id(doc_id: str | None) -> str:
    """Synthesize a Doc URL when Drive omits ``webViewLink`` (defensive)."""
    if not doc_id:
        return ""
    return f"https://docs.google.com/document/d/{doc_id}/edit"
