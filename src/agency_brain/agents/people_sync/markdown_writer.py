"""Compose AccountNote / ContactNote bundles from rows (ADR 0057 §2/§3).

Pure functions — no I/O. Turns a row dataclass into:
  - a filename (Drive-safe, derived from Airtable display name)
  - a frontmatter dict (the YAML keys + values)
  - a body skeleton (only used on first creation)

The body skeleton has fixed H2 section headers that the Phase 2/4
``bq_enricher`` later finds + populates. User prose lives between
sections and is never overwritten.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .models import AccountNote, AccountRow, ContactNote, ContactRow

# Drive doesn't allow these characters in filenames. Strip them; preserve
# everything else (case, spaces, accents) so the filename matches the
# user's mental model + the wikilink they type in briefs.
_DRIVE_ILLEGAL_CHARS = re.compile(r'[/:?*"<>|]')

# These are the H2 section headers the Phase 2/4 enricher targets. The
# AUTO comment markers tell readers (humans + future code) that the
# content between this header and the next H2 is regenerated each tick.
_AUTO_MARKER = "<!-- AUTO: populated by asb-people-sync -->"


def make_account_note(row: AccountRow) -> AccountNote:
    """Compose the Drive filename + frontmatter + body for one Account."""
    filename = _safe_filename(row.name)
    frontmatter = {
        "type": "account",
        "airtable_id": row.airtable_id,
        "name": row.name,
        "status": (row.status or "active").lower(),
        "hipaa": False,  # constant — HIPAA rows are filtered upstream
        "industry": row.industry or "",
        "account_manager": row.account_manager or "",
        "relationship_type": "client",
        "drive_folder_url": row.drive_folder_url or "",
        "synced_at": _now_iso(),
    }
    body = f"""# {row.name}

## Who they are
<!-- user prose; replace this line -->

## Active engagements
{_AUTO_MARKER}

## Recent activity
{_AUTO_MARKER}

## Open risks
{_AUTO_MARKER}

## Connections
<!-- user prose; add [[Contact Name]] wikilinks here -->
"""
    return AccountNote(filename=filename, frontmatter=frontmatter, body_skeleton=body)


def make_contact_note(row: ContactRow) -> ContactNote:
    """Compose the Drive filename + frontmatter + body for one Contact."""
    filename = _safe_filename(row.name)
    frontmatter = {
        "type": "person",
        "airtable_id": row.airtable_id,
        "name": row.name,
        "email": row.email or "",
        "role": row.role or "",
        "organization": row.organization or "",
        "relationship_type": row.relationship_type or "",
        "warmth": (row.warmth or "").lower(),
        "last_contact": row.last_contact.isoformat() if row.last_contact else "",
        "next_followup": row.next_followup.isoformat() if row.next_followup else "",
        "linkedin": row.linkedin or "",
        "phone": row.phone or "",
        "tags": [],  # reserved for future multi-select sync
        "synced_at": _now_iso(),
    }
    body = f"""# {row.name}

## Who they are
<!-- user prose; replace this line -->

## How I know them
<!-- user prose; replace this line -->

## Conversation log
{_AUTO_MARKER}

## Connections to my work
<!-- user prose; add wikilinks here -->
"""
    return ContactNote(filename=filename, frontmatter=frontmatter, body_skeleton=body)


def make_archived_frontmatter_update(
    existing_frontmatter: dict[str, Any], reason: str = "deleted from Airtable"
) -> dict[str, Any]:
    """Compute the frontmatter dict that flags a row as archived.

    ADR 0057 §4: when a Contact or Account is deleted in Airtable, the
    Brain file is NOT deleted. We flip ``status`` to ``archived`` and
    add an ``archived_at`` + ``archived_reason`` field. Other keys are
    preserved as-is so the historical context survives.
    """
    out = dict(existing_frontmatter)
    out["status"] = "archived"
    out["archived_at"] = _now_iso()
    out["archived_reason"] = reason
    out["synced_at"] = _now_iso()
    return out


def _safe_filename(name: str) -> str:
    """Convert an Airtable display name into a Drive-safe filename.

    Strips chars Drive doesn't allow (``/ : ? * " < > |``), collapses
    runs of whitespace, then appends ``.md``. Preserves case +
    Unicode + spaces so the filename matches what the user typed and
    the wikilink resolver (case-insensitive on filename, per ADR 0053)
    finds it.
    """
    cleaned = _DRIVE_ILLEGAL_CHARS.sub("", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "unnamed"
    return f"{cleaned}.md"


def _now_iso() -> str:
    """RFC 3339 UTC timestamp suitable for YAML, no microseconds."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
