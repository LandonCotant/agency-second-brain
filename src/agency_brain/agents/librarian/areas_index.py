"""Recursive listing of one or more destination roots for the classifier.

Originally this walked a single ``Brain/Areas/`` root. The user's wiki
doesn't actually live in Brain — their client work, ops, marketing, etc.
all live in the ``the agency`` Shared Drive. So the index now
takes a list of roots (``[(label, folder_id), ...]``) and unions their
candidate sets, prefixing each candidate's path with the root's label so
the classifier can disambiguate (e.g. ``brain/personal/wellness`` vs
``clients/clienta-pi/01_STRATEGY``).

Per-folder-name exclusion list lets you keep specific folders out of
the candidate set (e.g. ``02_FINANCE & ACCOUNTING``).
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import AreaFolder

log = logging.getLogger("agency_brain.agents.librarian.areas_index")


_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveListClient(Protocol):
    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]: ...


class LibrarianAreasIndex:
    """Walks one or more destination roots, returns ``AreaFolder`` candidates.

    ``_uncategorized`` is excluded universally — the classifier never
    proposes it; it's reserved for the low-confidence fallback in
    ``main.py``. Additional folder names can be excluded via
    ``excluded_names`` (case-insensitive match on the folder display
    name, applies at any depth).
    """

    DEFAULT_EXCLUDED_NAMES = frozenset({"_uncategorized"})

    def __init__(
        self,
        *,
        drive_client: DriveListClient,
        roots: (list[tuple[str, str]] | list[tuple[str, str, str]] | list[tuple] | None) = None,
        areas_root_id: str | None = None,
        excluded_names: frozenset[str] | None = None,
        max_depth: int = 3,
        page_size: int = 100,
    ) -> None:
        """``roots`` is the multi-root spec.

        Two accepted shapes per entry, both legal in the same list:
          - 2-tuple ``(label, folder_id)`` — legacy; ``bucket`` defaults
            to ``"areas"``.
          - 3-tuple ``(bucket, label, folder_id)`` — ADR 0054. ``bucket``
            is one of ``"areas"`` / ``"resources"`` and drives
            ``note_kind`` downstream in the ingestor.

        Backward-compat: passing ``areas_root_id`` (the legacy single-
        root form) is converted to ``roots=[("areas", "areas",
        areas_root_id)]``. At least one of the two must be set.
        """
        if roots is None and not areas_root_id:
            raise ValueError("either roots or areas_root_id must be set")
        if roots is None:
            roots = [("areas", "areas", areas_root_id or "")]
        # Normalize to 3-tuples (bucket, label, folder_id) and reject
        # empty folder ids — caller probably forgot to populate an env
        # var.
        cleaned: list[tuple[str, str, str]] = []
        for entry in roots:
            entry_t = tuple(entry)
            if len(entry_t) == 2:
                bucket = "areas"
                label, folder_id = entry_t
            elif len(entry_t) == 3:
                bucket, label, folder_id = entry_t
            else:
                log.warning(
                    "librarian.areas_index.bad_root_tuple entry=%r (need 2 or 3 items)", entry
                )
                continue
            bucket = (bucket or "areas").strip().lower() or "areas"
            label = (label or "").strip()
            folder_id = (folder_id or "").strip()
            if not folder_id:
                log.info("librarian.areas_index.skip_empty_root label=%r", label)
                continue
            cleaned.append((bucket, label or "root", folder_id))
        if not cleaned:
            raise ValueError("no usable destination roots after filtering empties")
        self._roots = cleaned
        self._drive = drive_client
        self._max_depth = max_depth
        self._page_size = page_size
        excluded_set: frozenset[str] = (
            self.DEFAULT_EXCLUDED_NAMES
            if excluded_names is None
            else (self.DEFAULT_EXCLUDED_NAMES | excluded_names)
        )
        self._excluded_lc = frozenset(name.strip().lower() for name in excluded_set if name.strip())

    @property
    def roots(self) -> list[tuple[str, str, str]]:
        return list(self._roots)

    def list_candidates(self) -> list[AreaFolder]:
        """Walk every configured root and union their candidate sets.

        Each root's candidates have ``root_label`` / ``root_id`` /
        ``bucket`` set so the mover can resolve the destination back to
        the right Drive, the classifier prompt can frame each
        candidate's universe, and the ingestor can map bucket → kind.
        Returns ``[]`` on full Drive failure so the classifier silently
        degrades to ``_uncategorized/`` rather than blocking the tick.
        """
        candidates: list[AreaFolder] = []
        for bucket, label, root_id in self._roots:
            try:
                self._walk(
                    parent_id=root_id,
                    parent_path=label,
                    root_id=root_id,
                    root_label=label,
                    bucket=bucket,
                    depth=0,
                    out=candidates,
                )
            except Exception:
                log.exception("librarian.areas_index.walk_failed label=%s root=%s", label, root_id)
        return candidates

    # ------------------------------------------------------------------ helpers

    def _walk(
        self,
        *,
        parent_id: str,
        parent_path: str,
        root_id: str,
        root_label: str,
        bucket: str,
        depth: int,
        out: list[AreaFolder],
    ) -> None:
        if depth >= self._max_depth:
            return
        try:
            raw_rows = self._drive.list_files(
                folder_id=parent_id,
                page_size=self._page_size,
                fields="files(id, name, mimeType)",
            )
        except Exception:
            log.exception("librarian.areas_index.list_failed parent=%s", parent_id)
            return
        for raw in raw_rows:
            if str(raw.get("mimeType") or "") != _GOOGLE_FOLDER_MIME:
                continue
            name = str(raw.get("name") or "").strip()
            if not name or name.startswith("."):
                continue
            if name.lower() in self._excluded_lc:
                continue
            folder_id = str(raw.get("id") or "")
            if not folder_id:
                continue
            path = f"{parent_path}/{name}" if parent_path else name
            out.append(
                AreaFolder(
                    id=folder_id,
                    name=name,
                    path=path,
                    root_id=root_id,
                    root_label=root_label,
                    bucket=bucket,
                )
            )
            self._walk(
                parent_id=folder_id,
                parent_path=path,
                root_id=root_id,
                root_label=root_label,
                bucket=bucket,
                depth=depth + 1,
                out=out,
            )


def find_folder_by_path(candidates: list[AreaFolder], path: str | None) -> AreaFolder | None:
    """Resolve a classifier's ``dest_folder_path`` string to an ``AreaFolder``.

    Path matching is case-insensitive and tolerant of leading/trailing
    slashes. Returns None if not found — the caller should fall back to
    ``_uncategorized/`` in the appropriate root.
    """
    if not path:
        return None
    target = path.strip().lower().strip("/")
    for folder in candidates:
        if folder.path.lower().strip("/") == target:
            return folder
    return None


def parse_roots_env(raw: str) -> list[tuple[str, str, str]]:
    """Parse ``LIBRARIAN_DEST_ROOTS`` env string into ``(bucket, label, folder_id)`` triples.

    Two entry shapes accepted, both legal in the same env string:
      - ``label:folder_id`` — legacy; ``bucket`` defaults to ``"areas"``.
      - ``bucket=label:folder_id`` — ADR 0054. ``bucket`` is one of
        ``"areas"`` / ``"resources"``.

    Whitespace around the separators is tolerated. Empty / malformed
    entries are skipped with a log warning rather than raising — bad
    env strings shouldn't break a daily tick.
    """
    out: list[tuple[str, str, str]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        bucket = "areas"
        if "=" in entry:
            bucket_raw, _, rest = entry.partition("=")
            bucket = bucket_raw.strip().lower() or "areas"
            entry = rest.strip()
        if ":" not in entry:
            log.warning("librarian.areas_index.bad_root_entry entry=%r (need label:id)", entry)
            continue
        label, _, folder_id = entry.partition(":")
        label = label.strip()
        folder_id = folder_id.strip()
        if not label or not folder_id:
            log.warning("librarian.areas_index.bad_root_entry label=%r id=%r", label, folder_id)
            continue
        out.append((bucket, label, folder_id))
    return out


def parse_excluded_names_env(raw: str) -> frozenset[str]:
    """Parse a comma-separated folder-name exclusion list.

    Whitespace is stripped per entry. Case-insensitive matching happens
    inside ``LibrarianAreasIndex._walk``.
    """
    parts: list[str] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if piece:
            parts.append(piece)
    return frozenset(parts) if parts else frozenset()
