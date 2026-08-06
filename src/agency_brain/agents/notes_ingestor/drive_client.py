"""Thin wrapper around the Drive v3 API for the notes ingestor.

ADR 0031: SA accesses each watched folder via direct folder share —
NOT Domain-Wide Delegation. Auth is ADC under the SA's own identity,
scoped to the Drive readonly OAuth scope at credential-build time.

Exposes the operations the main loop needs:

  list_new_pdfs(folder, since)               -> Iterator[DriveFile]
    Back-compat for ADR 0031 callers; PDF-only.
  list_new_files(folder, since, mime_types,  -> Iterator[DriveFile]
                 name_suffixes)
    ADR 0037 §5: multi-MIME listing for the IPARAG-adapted layout.
  download_pdf(file_id)                      -> bytes
    Back-compat for ADR 0031 callers; PDF-only.
  download_file(file_id, mime_type)          -> bytes
    ADR 0037 §5: routes Google Docs through ``export_media`` so the
    bytes returned are already ``text/markdown``.
  move_to_processed(file_id, ...)            -> None  (best-effort housekeeping)

A ``revision_id`` accompanies each listed file so the writer's dedup
key can include the head revision (ADR 0031 §4 — re-edited notes
produce new ``(file_id, revision_id)`` pairs).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .extractor import (
    AUDIO_MIME_TYPES,
    GOOGLE_DOC_MIME_TYPE,
    MARKDOWN_MIME_TYPES,
    PDF_MIME_TYPE,
)
from .models import DriveFile, NoteFolder

log = logging.getLogger("agency_brain.agents.notes_ingestor.drive_client")

# Drive readonly + write — write is needed only to move processed files
# into a `processed/` subfolder. We never modify or delete the user's
# original notes; only relocate them after a successful BQ write.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# Default MIME-type allowlist for ``list_new_files`` calls — covers
# every input shape the multi-MIME extractor knows about (ADR 0037 §5).
DEFAULT_BRAIN_MIME_TYPES: tuple[str, ...] = (
    PDF_MIME_TYPE,
    GOOGLE_DOC_MIME_TYPE,
    *MARKDOWN_MIME_TYPES,
    *AUDIO_MIME_TYPES,
)

# Filename-suffix backstop. Per ADR 0031's 2026-05-04 closeout addendum,
# Drive's mimeType auto-detection can be unreliable; matching the name
# suffix is the load-bearing check for files Drive mis-types.
DEFAULT_BRAIN_NAME_SUFFIXES: tuple[str, ...] = (
    ".pdf",
    ".md",
    ".m4a",
    ".mp3",
    ".wav",
)


class DriveServiceFactory(Protocol):
    """Builds a ``googleapiclient`` Drive v3 service object."""

    def build(self) -> Any: ...


@dataclass(frozen=True)
class FolderConfig:
    """Maps a watched folder id to its role + processed-subfolder.

    ADR 0048 extends this with optional context fields for Solutions
    Drive ingestion:

    - ``client_name``: the human-readable account name resolved from the
      client folder name (e.g., ``06_CLIENT_A`` → ``Client A``).
      Prepended to the extracted Markdown so /ask retrieval finds
      content by client name semantically.
    - ``source_path``: a slash-separated source attribution for the
      Markdown header (e.g., ``05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES``).
    - ``recursive``: when True, ``list_new_files`` descends into all
      subfolders of ``folder_id`` (BFS) and yields matching files from
      every descendant. Default False preserves ADR 0031/0037 behavior
      (list files directly inside ``folder_id`` only).
    """

    folder_id: str
    role: NoteFolder
    processed_subfolder_name: str = "processed"
    client_name: str | None = None
    source_path: str | None = None
    recursive: bool = False


class NotesDriveClient:
    """Drive operations against the watched folders.

    Pagination: each list call caps at ``page_size`` files; the main
    loop further caps total per-tick work via ``MAX_NOTES_PER_TICK``.
    """

    def __init__(
        self,
        *,
        service_factory: DriveServiceFactory,
        page_size: int = 100,
    ) -> None:
        self._factory = service_factory
        self._page_size = page_size
        self._service: Any = None

    def _svc(self) -> Any:
        if self._service is None:
            self._service = self._factory.build()
        return self._service

    # ---------------------------------------------------- listing API

    def list_new_pdfs(self, *, folder: FolderConfig, since: datetime | None) -> Iterator[DriveFile]:
        """ADR 0031 back-compat: PDF-only listing.

        Preserves the exact ``q=`` shape the existing test suite pins
        (``(mimeType = 'application/pdf' or name contains '.pdf')``).
        New callers should prefer ``list_new_files``.
        """
        # Hand-built clause to keep the test-pinned form.
        type_clause = "(mimeType = 'application/pdf' or name contains '.pdf')"
        return self._list_with_clause(folder=folder, since=since, type_clause=type_clause)

    def list_new_files(
        self,
        *,
        folder: FolderConfig,
        since: datetime | None,
        mime_types: Iterable[str] | None = None,
        name_suffixes: Iterable[str] | None = None,
    ) -> Iterator[DriveFile]:
        """ADR 0037 §5: multi-MIME listing.

        Yield files in `folder.folder_id` modified strictly after
        `since`, where the file's mimeType is in `mime_types` OR the
        filename ends with one of `name_suffixes` (the load-bearing
        backstop per ADR 0031 closeout — Drive's mimeType detection
        can mis-type, but the suffix is reliable).

        Defaults to the full Brain allowlist (PDF, Google Doc,
        Markdown, audio).
        """
        mt = tuple(mime_types) if mime_types is not None else DEFAULT_BRAIN_MIME_TYPES
        ns = tuple(name_suffixes) if name_suffixes is not None else DEFAULT_BRAIN_NAME_SUFFIXES

        clauses: list[str] = []
        for m in mt:
            # Drive's q syntax doesn't allow embedded single-quotes in
            # the literal; defensively skip any odd inputs.
            if "'" in m:
                continue
            clauses.append(f"mimeType = '{m}'")
        for s in ns:
            if "'" in s:
                continue
            clauses.append(f"name contains '{s}'")
        if not clauses:
            type_clause = "name != ''"  # vacuously true; lists all
        else:
            type_clause = "(" + " or ".join(clauses) + ")"

        return self._list_with_clause(folder=folder, since=since, type_clause=type_clause)

    def _list_with_clause(
        self,
        *,
        folder: FolderConfig,
        since: datetime | None,
        type_clause: str,
    ) -> Iterator[DriveFile]:
        """Shared listing pagination — used by both list_new_pdfs and list_new_files.

        When ``folder.recursive`` is True (ADR 0048), descends BFS into
        every subfolder of ``folder.folder_id`` and yields matching
        files from each. The watermark is still per-root, so the next
        tick listing will skip files modified ≤ watermark across the
        whole subtree.

        Drive Shared Drive support: every list call now passes
        ``supportsAllDrives=True`` + ``includeItemsFromAllDrives=True``
        so files in Shared Drives (the ``the agency`` root)
        return alongside files in shared My Drive folders. Behavior for
        existing Brain folders is unchanged.
        """
        if folder.recursive:
            parent_ids = list(self._walk_subfolders(folder.folder_id))
        else:
            parent_ids = [folder.folder_id]

        for parent_id in parent_ids:
            yield from self._list_in_one_parent(
                parent_id=parent_id,
                folder=folder,
                since=since,
                type_clause=type_clause,
            )

    def _list_in_one_parent(
        self,
        *,
        parent_id: str,
        folder: FolderConfig,
        since: datetime | None,
        type_clause: str,
    ) -> Iterator[DriveFile]:
        clauses = [
            f"'{parent_id}' in parents",
            "trashed = false",
            type_clause,
        ]
        if since is not None:
            ts = since.astimezone().isoformat()
            clauses.append(f"modifiedTime > '{ts}'")
        q = " and ".join(clauses)

        svc = self._svc()
        page_token: str | None = None
        while True:
            resp = (
                svc.files()
                .list(
                    q=q,
                    fields=(
                        "nextPageToken,"
                        "files(id,name,mimeType,modifiedTime,webViewLink,headRevisionId)"
                    ),
                    orderBy="modifiedTime asc",
                    pageSize=self._page_size,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files_in_page = resp.get("files", [])
            log.info(
                "drive.list parent=%s role=%s since=%s found=%d names=%s",
                parent_id,
                folder.role.value,
                since.isoformat() if since else None,
                len(files_in_page),
                [(f.get("name"), f.get("mimeType")) for f in files_in_page],
            )
            for f in files_in_page:
                # `headRevisionId` is documented for binary files (incl.
                # PDFs); fall back to `modifiedTime`-as-revision so the
                # dedup key is never empty.
                revision_id = f.get("headRevisionId") or _parse_ts(f["modifiedTime"]).isoformat()
                yield DriveFile(
                    file_id=f["id"],
                    revision_id=revision_id,
                    name=f["name"],
                    modified_time=_parse_ts(f["modifiedTime"]),
                    web_view_link=f.get("webViewLink", ""),
                    folder=folder.role,
                    mime_type=f.get("mimeType", ""),
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def _walk_subfolders(self, root_id: str) -> Iterator[str]:
        """Yield ``root_id`` and every descendant folder id (BFS).

        Used by recursive ``list_new_files`` (ADR 0048 §2 internal
        Solutions folder sweep). Folders without files still get yielded
        (no harm — the per-parent ``list`` call is bounded by
        ``page_size`` and returns zero matches for empty folders).
        """
        seen: set[str] = set()
        queue: list[str] = [root_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            yield current
            queue.extend(self._list_child_folder_ids(current))

    def _list_child_folder_ids(self, parent_id: str) -> list[str]:
        """List immediate-child folder ids of ``parent_id``.

        Internal helper for ``_walk_subfolders``. Filters on
        ``mimeType='application/vnd.google-apps.folder'`` so only
        sub-folders are returned, not files. Includes Shared Drive flags.
        """
        svc = self._svc()
        q = (
            f"'{parent_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        page_token: str | None = None
        ids: list[str] = []
        while True:
            resp = (
                svc.files()
                .list(
                    q=q,
                    fields="nextPageToken,files(id)",
                    pageSize=self._page_size,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in resp.get("files", []):
                ids.append(f["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def list_immediate_subfolders(self, parent_id: str) -> list[dict[str, str]]:
        """ADR 0048 §3 — discovery step for Solutions client sweep.

        Returns one dict ``{"id": ..., "name": ...}`` per immediate-child
        folder of ``parent_id``. Used to:

        1. Discover client folders under ``05_CLIENTS/`` (so HIPAA
           filtering + ``00_CLIENT_TEMPLATE`` skip happens before walking).
        2. Discover allowlisted subfolders under each client folder.

        Excludes trashed folders. Includes Shared Drive items.
        """
        svc = self._svc()
        q = (
            f"'{parent_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        page_token: str | None = None
        out: list[dict[str, str]] = []
        while True:
            resp = (
                svc.files()
                .list(
                    q=q,
                    fields="nextPageToken,files(id,name)",
                    pageSize=self._page_size,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in resp.get("files", []):
                out.append({"id": f["id"], "name": f["name"]})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    # ---------------------------------------------------- download API

    def download_pdf(self, file_id: str) -> bytes:
        """ADR 0031 back-compat: PDF binary download."""
        return self.download_file(file_id=file_id, mime_type=PDF_MIME_TYPE)

    def download_file(self, *, file_id: str, mime_type: str) -> bytes:
        """ADR 0037 §5: route Google Docs through ``export_media`` and
        binary types through ``get_media``.

        Google Docs cannot be downloaded directly — Drive requires
        ``export_media(mimeType='text/markdown')``. The exported bytes
        are clean Markdown, so the extractor's passthrough path
        consumes them without an LLM call (ADR 0037 §5).
        """
        svc = self._svc()
        if mime_type == GOOGLE_DOC_MIME_TYPE:
            return svc.files().export_media(fileId=file_id, mimeType="text/markdown").execute()
        # `get_media` doesn't accept supportsAllDrives in the v3 client; Shared
        # Drive items download fine via this path once the file is listable.
        return svc.files().get_media(fileId=file_id).execute()

    # ---------------------------------------------------- housekeeping

    def move_to_processed(self, *, file_id: str, current_parent: str, folder: FolderConfig) -> str:
        """Move `file_id` into the `processed/` subfolder of `folder`.

        Returns the destination folder id. Idempotent: looks up (or
        creates) the subfolder by name + parent.

        Best-effort: a failure here logs but does not roll back the BQ
        write. The dedup key prevents re-ingestion if the move fails;
        the user can clean up manually.

        ADR 0048: callers gate this on ``should_move_to_processed(role)``
        to skip the move for Solutions reference files.
        """
        svc = self._svc()
        processed_id = self._ensure_subfolder(
            svc, parent_id=folder.folder_id, name=folder.processed_subfolder_name
        )
        svc.files().update(
            fileId=file_id,
            addParents=processed_id,
            removeParents=current_parent,
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
        return processed_id

    def _ensure_subfolder(self, svc: Any, *, parent_id: str, name: str) -> str:
        """Find or create a folder named `name` under `parent_id`."""
        q = (
            f"'{parent_id}' in parents and name = '{name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        existing = (
            svc.files()
            .list(
                q=q,
                fields="files(id)",
                pageSize=2,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )
        if existing:
            return existing[0]["id"]
        created = (
            svc.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return created["id"]


class ADCDriveServiceFactory:
    """Builds a Drive v3 service via Application Default Credentials.

    Production path: Cloud Run Job ADC = the SA the job runs as
    (``asb-notes-ingestor-sa``). Folders are shared with that SA email.
    No DWD; no impersonation. ``cache_discovery=False`` keeps cold-start
    light.
    """

    def __init__(self, *, scope: str = DRIVE_SCOPE) -> None:
        self._scope = scope

    def build(self) -> Any:  # pragma: no cover — exercised in integ
        from google.auth import default
        from googleapiclient.discovery import build

        creds, _ = default(scopes=[self._scope])
        return build("drive", "v3", credentials=creds, cache_discovery=False)


def _parse_ts(raw: str) -> datetime:
    """Parse a Drive RFC 3339 timestamp into a tz-aware datetime."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
