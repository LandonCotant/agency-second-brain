"""Drive file mover for the Librarian (ADR 0044).

Single-purpose: ``drive.files.update`` with ``addParents`` +
``removeParents`` + ``supportsAllDrives=True``. Handles the
``_uncategorized/`` auto-create idempotently so a no-confident-match
file never disappears into a deleted location.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("agency_brain.agents.librarian.mover")


class DriveServiceFactory(Protocol):
    def build(self) -> Any: ...


class LibrarianMoveError(RuntimeError):
    pass


@dataclass(frozen=True)
class MoveResult:
    file_id: str
    from_folder_id: str
    to_folder_id: str
    to_folder_path: str


class LibrarianMover:
    """Move a Drive file from its current Inbox folder to an Areas folder."""

    def __init__(
        self,
        *,
        service_factory: DriveServiceFactory,
        areas_root_id: str,
        uncategorized_name: str = "_uncategorized",
    ) -> None:
        if not areas_root_id:
            raise ValueError("areas_root_id is required")
        self._factory = service_factory
        self._areas_root = areas_root_id
        self._uncategorized_name = uncategorized_name
        self._svc: Any | None = None
        self._uncategorized_id: str | None = None

    def move(
        self,
        *,
        file_id: str,
        from_folder_id: str,
        to_folder_id: str,
        to_folder_path: str,
    ) -> MoveResult:
        """Move a file via ``files.update(addParents/removeParents)``.

        Cross-Shared-Drive moves can fail with 403 on certain files
        (shortcuts, files owned by an external user, files in a Shared
        Drive that has the "members can move out" setting off). When
        that happens, fall back to ``files.copy`` into the destination
        + leave the original in place — the SA can copy content to a
        Drive it's a member of even when it can't strip the source's
        ownership. Caller / human cleans up the source manually.
        """
        svc = self._get_service()
        from googleapiclient.errors import HttpError

        try:
            svc.files().update(
                fileId=file_id,
                addParents=to_folder_id,
                removeParents=from_folder_id,
                fields="id, parents",
                supportsAllDrives=True,
            ).execute()
            return MoveResult(
                file_id=file_id,
                from_folder_id=from_folder_id,
                to_folder_id=to_folder_id,
                to_folder_path=to_folder_path,
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status == 403:
                log.warning(
                    "librarian.mover.update_403_fallback file=%s — trying files.copy",
                    file_id,
                )
                try:
                    copied = (
                        svc.files()
                        .copy(
                            fileId=file_id,
                            body={"parents": [to_folder_id]},
                            fields="id",
                            supportsAllDrives=True,
                        )
                        .execute()
                    )
                    new_id = str(copied.get("id") or "")
                    if not new_id:
                        raise LibrarianMoveError(f"files.copy returned no id for {file_id}")
                    log.info(
                        "librarian.mover.copy_succeeded src=%s copy=%s",
                        file_id,
                        new_id,
                    )
                    return MoveResult(
                        file_id=new_id,
                        from_folder_id=from_folder_id,
                        to_folder_id=to_folder_id,
                        to_folder_path=to_folder_path,
                    )
                except LibrarianMoveError:
                    raise
                except Exception as copy_exc:
                    raise LibrarianMoveError(
                        f"drive.files.update 403 for {file_id} AND copy fallback failed: "
                        f"{type(copy_exc).__name__}: {copy_exc}"
                    ) from copy_exc
            raise LibrarianMoveError(
                f"drive.files.update failed for {file_id}: HTTP {status}: {exc}"
            ) from exc
        except Exception as exc:
            raise LibrarianMoveError(
                f"drive.files.update failed for {file_id}: {type(exc).__name__}: {exc}"
            ) from exc

    def rename_file(self, *, file_id: str, new_name: str) -> bool:
        """Apply ``files.update(name=new_name)`` to the destination file.

        Same-Drive metadata change; doesn't hit the cross-drive 403
        path. Returns True on success, False on any failure (logged).
        Caller surfaces success in the audit row.
        """
        if not file_id or not new_name:
            return False
        svc = self._get_service()
        try:
            svc.files().update(
                fileId=file_id,
                body={"name": new_name},
                fields="id, name",
                supportsAllDrives=True,
            ).execute()
            return True
        except Exception:
            log.exception(
                "librarian.mover.rename_failed file=%s new_name=%r",
                file_id,
                new_name,
            )
            return False

    def archive_processed_original(self, *, file_id: str, source_folder_id: str) -> bool:
        """Move the source file into a ``processed/`` subfolder of its source.

        Used after a successful copy-fallback move (cross-Shared-Drive
        moves can leave the original in the source folder). The
        ``processed/`` subfolder is auto-created idempotently. Same-Drive
        moves don't hit the cross-drive 403 path so this is the cheap
        cleanup option. Returns True on success, False on any failure
        (permission, race condition, etc.) so the caller can keep going
        — the audit-log skip-list (Phase G+) still prevents re-processing.
        """
        if not file_id or not source_folder_id:
            return False
        svc = self._get_service()
        try:
            processed_id = self._lookup_subfolder(svc, source_folder_id, "processed")
            if not processed_id:
                created = (
                    svc.files()
                    .create(
                        body={
                            "name": "processed",
                            "mimeType": "application/vnd.google-apps.folder",
                            "parents": [source_folder_id],
                        },
                        fields="id",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                processed_id = str(created.get("id") or "")
            if not processed_id:
                return False
            svc.files().update(
                fileId=file_id,
                addParents=processed_id,
                removeParents=source_folder_id,
                fields="id, parents",
                supportsAllDrives=True,
            ).execute()
            return True
        except Exception:
            log.exception(
                "librarian.mover.archive_processed_failed file=%s src=%s",
                file_id,
                source_folder_id,
            )
            return False

    def get_or_create_uncategorized(self) -> str:
        """Idempotent ``Brain/Areas/_uncategorized/`` lookup-or-create."""
        if self._uncategorized_id is not None:
            return self._uncategorized_id
        svc = self._get_service()
        # Look up first.
        existing = self._lookup_subfolder(svc, self._areas_root, self._uncategorized_name)
        if existing:
            self._uncategorized_id = existing
            return existing
        try:
            created = (
                svc.files()
                .create(
                    body={
                        "name": self._uncategorized_name,
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": [self._areas_root],
                    },
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            raise LibrarianMoveError(
                f"Failed to create _uncategorized/ folder: {type(exc).__name__}: {exc}"
            ) from exc
        new_id = created.get("id")
        if not new_id:
            raise LibrarianMoveError("Drive returned no id for new _uncategorized/ folder")
        self._uncategorized_id = new_id
        return new_id

    # ------------------------------------------------------------------ helpers

    def _get_service(self) -> Any:
        if self._svc is None:
            self._svc = self._factory.build()
        return self._svc

    def _lookup_subfolder(self, svc: Any, parent_id: str, name: str) -> str | None:
        # ``q=`` lookup; escapes single quotes in folder name.
        safe_name = name.replace("'", "\\'")
        q = (
            f"mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false "
            f"and '{parent_id}' in parents "
            f"and name = '{safe_name}'"
        )
        try:
            resp = (
                svc.files()
                .list(
                    q=q,
                    fields="files(id, name)",
                    pageSize=10,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception:
            log.exception(
                "librarian.mover.lookup_subfolder_failed parent=%s name=%s", parent_id, name
            )
            return None
        for entry in resp.get("files", []) or []:
            return str(entry.get("id"))
        return None
