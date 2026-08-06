"""Lists Drive files in the Inbox folders the Librarian operates on.

Watched folders are passed in by ``main.py`` via env vars; the lister
takes them as ``(folder_id, role)`` tuples. ``role`` is the semantic
slot — ``drop`` (always processed) or ``quicknotes`` (optional age-based
sweep). HIPAA folders are NEVER threaded into this lister; ADR 0044 §4
keeps both the env-var allowlist and a code-side guard out of HIPAA
territory entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from .models import DropFile

log = logging.getLogger("agency_brain.agents.librarian.drive_lister")


# Roles the Librarian REFUSES to operate on, even if env vars route a
# folder id to them. ADR 0044 §4 — defense in depth: env-var allowlist
# excludes HIPAA folders AND the lister fails closed if asked.
_FORBIDDEN_FOLDER_ROLES = frozenset({"hipaa"})


class DriveListClient(Protocol):
    """Minimal Drive surface used by the lister."""

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]: ...


class LibrarianListError(RuntimeError):
    pass


class LibrarianDriveLister:
    """Builds the per-tick file list for the Librarian.

    Drop files are always returned (they're the user's "I don't know
    where this goes" signal). QuickNotes files are gated on age — only
    files older than ``quicknotes_age_days`` (default 14) get swept,
    so the user has a window to manually file fresh captures before
    the Librarian classifies them.
    """

    def __init__(
        self,
        *,
        drive_client: DriveListClient,
        max_per_tick: int = 50,
        quicknotes_age_days: int = 14,
        now: datetime | None = None,
        skip_file_ids: set[str] | None = None,
    ) -> None:
        self._drive = drive_client
        self._max = max_per_tick
        self._quicknotes_age_days = quicknotes_age_days
        self._now_override = now
        self._skip_ids = skip_file_ids or set()

    def list(self, watched: list[tuple[str, str]]) -> list[DropFile]:
        """Iterate the watched folders, returning a capped, ordered list.

        ``watched`` is ``[(folder_id, role), ...]``. Role must be one of
        ``drop``, ``quicknotes``. Anything in ``_FORBIDDEN_FOLDER_ROLES``
        raises ``LibrarianListError`` — there's no way the caller meant
        to ask the Librarian to touch that folder.
        """
        files: list[DropFile] = []
        seen: set[str] = set()
        cutoff = self._cutoff_for_quicknotes()
        for folder_id, role in watched:
            if not folder_id:
                continue
            if role in _FORBIDDEN_FOLDER_ROLES:
                raise LibrarianListError(
                    f"Librarian refuses folder role={role!r} (forbidden by ADR 0044 §4)"
                )
            try:
                raw_rows = self._drive.list_files(
                    folder_id=folder_id,
                    page_size=self._max,
                    fields="files(id, name, mimeType, parents, modifiedTime, webViewLink)",
                )
            except Exception:
                log.exception("librarian.list_files_failed folder=%s role=%s", folder_id, role)
                continue
            for raw in raw_rows:
                file_id = str(raw.get("id") or "")
                if not file_id or file_id in seen:
                    continue
                if file_id in self._skip_ids:
                    # Audit log says we've successfully classified+moved
                    # this file already (Phase G+ skip-list). The cross-
                    # Shared-Drive copy fallback leaves the original in
                    # Drop; without this guard, every tick creates another
                    # copy at the destination. Manual cleanup deletes the
                    # original to fully resolve.
                    log.info(
                        "librarian.skip_already_processed file_id=%s name=%r",
                        file_id,
                        raw.get("name"),
                    )
                    continue
                modified = _parse_ts(raw.get("modifiedTime") or "")
                if role == "quicknotes" and cutoff is not None and modified > cutoff:
                    continue  # too fresh; let the user file it manually
                if _is_folder(raw):
                    continue  # never sort sub-folders
                files.append(
                    DropFile(
                        file_id=file_id,
                        name=str(raw.get("name") or "(untitled)"),
                        mime_type=str(raw.get("mimeType") or ""),
                        parent_folder_id=folder_id,
                        parent_folder_role=role,
                        modified_time=modified,
                        web_view_link=raw.get("webViewLink") or None,
                    )
                )
                seen.add(file_id)
                if len(files) >= self._max:
                    return files
        return files

    def _cutoff_for_quicknotes(self) -> datetime | None:
        if self._quicknotes_age_days <= 0:
            return None
        from datetime import timedelta

        now = self._now_override or datetime.now().astimezone()
        return now - timedelta(days=self._quicknotes_age_days)


def _parse_ts(raw: str) -> datetime:
    if not raw:
        return datetime.fromtimestamp(0).astimezone()
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _is_folder(raw: dict[str, Any]) -> bool:
    return str(raw.get("mimeType") or "") == "application/vnd.google-apps.folder"
