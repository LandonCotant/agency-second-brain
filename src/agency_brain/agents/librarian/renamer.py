"""Filename canonicalization for the Librarian (Phase G+).

Format: ``YYYY-MM-DD_<topic-slug>_<description>.<ext>``

Only renames files whose original name matches an "anonymous" pattern
(voice-memo timestamps, ``Untitled.*``, ``Document (5).*``, generic
``IMG_*``, etc.). Files the user named deliberately are preserved.

Topic slug derives from the destination folder path the classifier
picked: take the SECOND-to-last segment of the path (which is usually
the client / topic folder, e.g. ``clients/06_CLIENT_A/08_MEETING
NOTES`` → ``client-a``). Strip number prefixes, lowercase, replace
underscores / spaces with hyphens.

Description comes from the classifier's optional ``suggested_description``
output (added to the response_schema in this phase). When absent, falls
back to a generic by file kind (``memo`` / ``note`` / ``recording``).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath

log = logging.getLogger("agency_brain.agents.librarian.renamer")


# Patterns identifying "anonymous" filenames the user almost certainly
# didn't craft. Matching is case-insensitive on the stem (the part
# before the final extension).
_ANONYMOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^untitled(\s*\(\d+\))?$", re.IGNORECASE),
    re.compile(r"^untitled[\s_-]?document(\s*\(\d+\))?$", re.IGNORECASE),
    re.compile(r"^document(\s*\(\d+\))?$", re.IGNORECASE),
    re.compile(r"^notes_\d{6}_\d{6}$", re.IGNORECASE),
    # Voice memo: voice-memo-N, voice_memo_N, voicememo<digits>
    re.compile(r"^voice[\s_-]?memo[\s_-]*\d*$", re.IGNORECASE),
    # Recording: Recording-N, recording_N
    re.compile(r"^recording[\s_-]*\d*$", re.IGNORECASE),
    # Image: IMG_1234, IMG-1234, image_1234
    re.compile(r"^img[\s_-]?\d+$", re.IGNORECASE),
    re.compile(r"^image[\s_-]*\d+$", re.IGNORECASE),
    # Screenshot — covers both bare and Apple-style "Screenshot 2026-05-07 at 1.53.42 PM"
    re.compile(r"^screen[\s_-]?shot.*$", re.IGNORECASE),
    # Timestamp-only: 2024-05-07_103045 / 20240507_103045
    re.compile(r"^\d{4}[\s_-]?\d{2}[\s_-]?\d{2}[\s_-]\d{2,6}$", re.IGNORECASE),
    # Generic: just an extension (.md), or a single-letter name
    re.compile(r"^.{0,2}$"),
)


# Sub-template folder names that are too generic to be the topic. When
# the destination's leaf folder matches one of these, walk up one level
# for the topic. Conservative list — extend as needed.
_GENERIC_LEAF_FOLDERS: frozenset[str] = frozenset(
    {
        "00_onboarding",
        "01_strategy",
        "02_legal_agreements",
        "02_legal-agreements",
        "03_client_brand",
        "03_client-brand",
        "04_creative",
        "05_campaigns",
        "06_deliverables",
        "07_reporting",
        "08_meeting_notes",
        "08_meeting notes",
        "08_meetings",
        "09_agent_workspace",
        "09_agent-workspace",
        "_uncategorized",
        "drop",
        "06_drop",
        "01_reflections",
        "reflections",
        "processed",
    }
)


def is_anonymous_filename(name: str) -> bool:
    """Return True if ``name`` looks auto-generated rather than crafted."""
    if not name:
        return True
    stem = _stem(name)
    if not stem:
        return True
    norm = stem.strip().lower()
    return any(pat.fullmatch(stem) for pat in _ANONYMOUS_PATTERNS) or norm in {
        "untitled",
        "document",
        "untitled document",
    }


def derive_topic_slug(dest_folder_path: str) -> str:
    """Pick the topic segment from a destination path and slugify it.

    For ``clients/06_CLIENT_A/08_MEETING NOTES`` returns
    ``client-a``. For ``playbooks/local-service`` returns
    ``local-service``. For just ``clients`` returns ``clients``.

    Walks the path from leaf to root, skipping segments in
    ``_GENERIC_LEAF_FOLDERS`` (sub-template folders that aren't the
    topic). Falls back to the first non-empty segment if everything is
    generic.
    """
    if not dest_folder_path:
        return "uncategorized"
    segments = [s for s in PurePosixPath(dest_folder_path.strip()).parts if s and s != "/"]
    for seg in reversed(segments):
        if seg.lower() in _GENERIC_LEAF_FOLDERS:
            continue
        slug = _slugify(seg)
        if slug:
            return slug
    # Everything was generic — slugify the leaf as last resort.
    if segments:
        return _slugify(segments[-1]) or "uncategorized"
    return "uncategorized"


def derive_description_slug(suggested: str | None, *, fallback: str = "note") -> str:
    """Slugify the classifier's suggested description; fallback on empty."""
    if suggested:
        s = _slugify(suggested)
        if s:
            return s[:50]  # cap length to keep filenames sane
    return fallback


def canonical_filename(
    *,
    date: datetime,
    topic: str,
    description: str,
    extension: str,
) -> str:
    """Compose the canonical filename per the user's chosen convention.

    Format: ``YYYY-MM-DD_<topic>_<description>.<ext>``.
    Extension is normalized lower-case, no leading dot.
    """
    date_str = date.astimezone(UTC).strftime("%Y-%m-%d")
    ext = (extension or "").strip().lstrip(".").lower()
    parts = [date_str, topic or "uncategorized", description or "note"]
    base = "_".join(p for p in parts if p)
    if ext:
        return f"{base}.{ext}"
    return base


def maybe_rename(
    *,
    original_name: str,
    dest_folder_path: str,
    file_modified_time: datetime,
    suggested_description: str | None = None,
    fallback_description: str = "note",
) -> str | None:
    """Compute a new filename if the original looks anonymous; else None.

    Returns ``None`` when the original filename should be preserved.
    Returns the canonical name otherwise. Caller applies the rename via
    ``drive.files.update(name=new_name)``.
    """
    if not is_anonymous_filename(original_name):
        return None
    ext = _extension(original_name)
    topic = derive_topic_slug(dest_folder_path)
    description = derive_description_slug(suggested_description, fallback=fallback_description)
    return canonical_filename(
        date=file_modified_time,
        topic=topic,
        description=description,
        extension=ext,
    )


# --------------------------------------------------------------- helpers


def _stem(name: str) -> str:
    """Extract the filename stem (without extension)."""
    if not name:
        return ""
    p = PurePosixPath(name)
    # Multi-extension files like "foo.tar.gz" — take the stem before
    # the last dot only. Good enough for our purposes.
    return p.stem


def _extension(name: str) -> str:
    if not name:
        return ""
    p = PurePosixPath(name)
    return p.suffix.lstrip(".").lower()


def _slugify(text: str) -> str:
    """Lowercase, strip leading number prefixes (``06_``), replace
    non-alphanumeric runs with single hyphen, trim."""
    if not text:
        return ""
    # Strip leading digit-prefix like "06_" or "00-".
    cleaned = re.sub(r"^\d+[\s_-]+", "", text.strip())
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned
