"""Drive folder-share + ADC writer for People Sync (ADR 0057 §1, §8).

Manages markdown files under ``Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/``:

  - **List + index by airtable_id**: read existing files in the parent
    folder; parse frontmatter; build ``{airtable_id → fileId}`` map.
  - **Upsert one note**: if airtable_id is unknown → create new file;
    else parse existing → diff frontmatter → only rewrite if anything
    changed. Body is read + preserved when rewriting (ADR 0057 §4: body
    never overwritten after first creation outside ``<!-- AUTO -->``
    blocks which Phase 2/4 handle separately).
  - **Mark as archived**: read existing → flip frontmatter status →
    rewrite (body untouched).

The SA accesses Drive under its own identity (no DWD; ADR 0044). The
parent folder must be shared with the SA's email as Content Manager
before any write succeeds.

Tests inject a fake ``DriveServiceFactory`` mirroring the pattern in
``common/drive_doc_writer.py``.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .frontmatter import compose, diff_relevant, parse_file
from .markdown_writer import make_archived_frontmatter_update
from .models import UpsertOutcome
from .sections import replace_auto_section

log = logging.getLogger("agency_brain.agents.people_sync.drive_writer")

MARKDOWN_MIME = "text/markdown"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveWriteError(RuntimeError):
    """Drive write failed."""


class DriveServiceFactory(Protocol):
    def build(self) -> Any: ...


@dataclass(frozen=True)
class ExistingFile:
    file_id: str
    name: str
    airtable_id: str | None
    """Parsed from the file's frontmatter. None means we couldn't parse
    (legacy file? user-deleted frontmatter?). Such files are left alone
    and not matched against incoming rows."""
    raw_content: str
    """Full file content (frontmatter + body)."""


class PeopleDriveWriter:
    """Drive upserts for one folder (e.g. ``05_GALAXY/01_ACCOUNTS/``).

    Construct one per target folder so the listing cache stays local
    to that folder.
    """

    def __init__(
        self,
        *,
        service_factory: DriveServiceFactory,
        parent_folder_id: str,
    ) -> None:
        self._factory = service_factory
        self._parent_folder_id = parent_folder_id
        self._svc: Any | None = None
        self._cache: dict[str, ExistingFile] | None = None
        """``airtable_id → ExistingFile``, populated on first list_existing call."""
        self._filename_index: dict[str, ExistingFile] | None = None
        """``filename → ExistingFile`` for collision detection."""

    # ----------------------------------------------------------- public

    def list_existing(self) -> dict[str, ExistingFile]:
        """Return ``{airtable_id → ExistingFile}`` for every .md in the parent.

        Files whose frontmatter lacks ``airtable_id`` (corrupted, hand-
        created, or pre-sync legacy) are skipped from the cache but
        still indexed by filename for collision detection.
        """
        if self._cache is not None:
            return dict(self._cache)
        files = self._list_md_files()
        cache: dict[str, ExistingFile] = {}
        fname_idx: dict[str, ExistingFile] = {}
        for f in files:
            raw = self._download(f["id"])
            fm, _ = parse_file(raw)
            airtable_id = fm.get("airtable_id") if isinstance(fm, dict) else None
            ex = ExistingFile(
                file_id=str(f["id"]),
                name=str(f["name"]),
                airtable_id=str(airtable_id) if airtable_id else None,
                raw_content=raw,
            )
            if ex.airtable_id:
                cache[ex.airtable_id] = ex
            fname_idx[ex.name] = ex
        self._cache = cache
        self._filename_index = fname_idx
        log.info(
            "people_sync.drive.listed parent=%s total=%d matched_by_airtable_id=%d",
            self._parent_folder_id,
            len(files),
            len(cache),
        )
        return dict(cache)

    def upsert(
        self,
        *,
        filename: str,
        airtable_id: str,
        new_frontmatter: dict[str, Any],
        body_skeleton: str,
        auto_sections: dict[str, str] | None = None,
    ) -> UpsertOutcome:
        """Create or update one .md file.

        Match resolution order (per ADR 0057 §1 collision rules):
          1. airtable_id match — keep the existing file, rewrite
             frontmatter + optionally refresh AUTO sections, preserve user prose.
          2. No airtable_id match + filename free → create new.
          3. No airtable_id match + filename TAKEN by a different
             airtable_id → use disambiguator.

        ``auto_sections`` (Phase 2, ADR 0057 §5): maps ``"## Header"`` →
        new section body. When provided, each named section's content
        is rewritten (in both new-file and update paths). Sections not
        listed here are unchanged. User-prose sections (no AUTO marker)
        are protected by ``replace_auto_section`` regardless of what's
        passed.
        """
        existing = self.list_existing()
        prior = existing.get(airtable_id)

        if prior is not None:
            return self._update_existing(prior, new_frontmatter, auto_sections)

        # New row. Resolve filename collision.
        fname_idx = self._filename_index or {}
        if filename in fname_idx and fname_idx[filename].airtable_id != airtable_id:
            stem, _, ext = filename.rpartition(".")
            short = airtable_id[-6:] if len(airtable_id) >= 6 else airtable_id
            filename = f"{stem} (collision-{short}).{ext}"
            log.warning(
                "people_sync.drive.filename_collision new=%s airtable_id=%s",
                filename,
                airtable_id,
            )

        # Apply AUTO sections to the skeleton before composing.
        body = body_skeleton
        if auto_sections:
            for header, section_content in auto_sections.items():
                body = replace_auto_section(body, header, section_content)
        content = compose(new_frontmatter, body)
        try:
            file_id = self._create_md(name=filename, content=content)
        except Exception as exc:
            log.exception("people_sync.drive.create_failed name=%s", filename)
            return UpsertOutcome(
                filename=filename,
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        # Update local cache so subsequent upserts in this run see it.
        new_ex = ExistingFile(
            file_id=file_id,
            name=filename,
            airtable_id=airtable_id,
            raw_content=content,
        )
        if self._cache is not None:
            self._cache[airtable_id] = new_ex
        if self._filename_index is not None:
            self._filename_index[filename] = new_ex

        return UpsertOutcome(
            filename=filename,
            created=True,
            frontmatter_updated=False,
            skipped_unchanged=False,
        )

    def mark_archived(
        self, *, airtable_id: str, reason: str = "deleted from Airtable"
    ) -> UpsertOutcome | None:
        """Flip an existing file's frontmatter to status: archived.

        Returns None if the file doesn't exist (idempotent — archiving
        a non-existent row is a no-op). Body is untouched.
        """
        existing = self.list_existing()
        prior = existing.get(airtable_id)
        if prior is None:
            return None

        fm, body = parse_file(prior.raw_content)
        if fm.get("status") == "archived":
            return UpsertOutcome(
                filename=prior.name,
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=True,
            )
        new_fm = make_archived_frontmatter_update(fm, reason=reason)
        new_content = compose(new_fm, body)
        try:
            self._update_md(file_id=prior.file_id, content=new_content)
        except Exception as exc:
            log.exception("people_sync.drive.archive_failed name=%s", prior.name)
            return UpsertOutcome(
                filename=prior.name,
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return UpsertOutcome(
            filename=prior.name,
            created=False,
            frontmatter_updated=True,
            skipped_unchanged=False,
        )

    # ----------------------------------------------------------- helpers

    def _update_existing(
        self,
        prior: ExistingFile,
        new_frontmatter: dict[str, Any],
        auto_sections: dict[str, str] | None,
    ) -> UpsertOutcome:
        old_fm, body = parse_file(prior.raw_content)

        # Apply AUTO sections to existing body (Phase 2).
        new_body = body
        if auto_sections:
            for header, section_content in auto_sections.items():
                new_body = replace_auto_section(new_body, header, section_content)

        body_changed = new_body != body
        fm_changed = diff_relevant(new_frontmatter, old_fm)
        if not fm_changed and not body_changed:
            return UpsertOutcome(
                filename=prior.name,
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=True,
            )
        new_content = compose(new_frontmatter, new_body)
        try:
            self._update_md(file_id=prior.file_id, content=new_content)
        except Exception as exc:
            log.exception("people_sync.drive.update_failed name=%s", prior.name)
            return UpsertOutcome(
                filename=prior.name,
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return UpsertOutcome(
            filename=prior.name,
            created=False,
            frontmatter_updated=True,
            skipped_unchanged=False,
        )

    def _get_service(self) -> Any:
        if self._svc is None:
            self._svc = self._factory.build()
        return self._svc

    def _list_md_files(self) -> list[dict]:
        svc = self._get_service()
        from googleapiclient.errors import HttpError

        q = (
            f"'{self._parent_folder_id}' in parents "
            f"and trashed = false "
            f"and (mimeType = '{MARKDOWN_MIME}' or name contains '.md')"
        )
        # Paginate: a single page caps at 1000 (we request 500). Without
        # following nextPageToken, files past the cap look 'missing' to
        # list_existing and the upsert creates a NEW dossier for each —
        # silent duplication of every account/contact beyond the page.
        files: list[dict] = []
        page_token: str | None = None
        try:
            while True:
                response = (
                    svc.files()
                    .list(
                        q=q,
                        pageSize=500,
                        fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                files.extend(response.get("files", []) or [])
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            raise DriveWriteError(f"list failed: {exc}") from exc
        return files

    def _download(self, file_id: str) -> str:
        svc = self._get_service()
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        try:
            request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")
        except HttpError as exc:
            raise DriveWriteError(f"download failed for {file_id}: {exc}") from exc

    def _create_md(self, *, name: str, content: str) -> str:
        svc = self._get_service()
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaInMemoryUpload

        body_bytes = content.encode("utf-8")
        media = MediaInMemoryUpload(body_bytes, mimetype=MARKDOWN_MIME)
        try:
            response = (
                svc.files()
                .create(
                    body={
                        "name": name,
                        "mimeType": MARKDOWN_MIME,
                        "parents": [self._parent_folder_id],
                    },
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveWriteError(f"create failed for {name}: {exc}") from exc
        file_id = str(response.get("id") or "")
        if not file_id:
            raise DriveWriteError(f"create returned no id for {name}")
        return file_id

    def _update_md(self, *, file_id: str, content: str) -> None:
        svc = self._get_service()
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaInMemoryUpload

        body_bytes = content.encode("utf-8")
        media = MediaInMemoryUpload(body_bytes, mimetype=MARKDOWN_MIME)
        try:
            svc.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveWriteError(f"update failed for {file_id}: {exc}") from exc
