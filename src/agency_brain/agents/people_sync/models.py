"""Dataclasses for the People Sync agent (ADR 0057).

Source rows (``AccountRow`` / ``ContactRow``) mirror the BQ
``airtable_replica`` schemas. Output bundles (``AccountNote`` /
``ContactNote``) carry the frontmatter dict + body markdown that the
``drive_writer`` upserts to ``Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/``.

Frontmatter is a plain ``dict[str, Any]`` rather than a strict dataclass
so adding fields later (Met date, Met context, etc.) doesn't require a
schema migration. Idempotency is computed by diffing the new vs.
existing frontmatter dict — only keys that differ trigger a Drive
rewrite (see ``frontmatter.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any


@dataclass(frozen=True)
class AccountRow:
    """One row from ``airtable_replica.accounts``. ADR 0057 §2.

    HIPAA-flagged rows are excluded at query time (``WHERE hipaa = false
    AND hipaa_excluded = false``), so a non-None ``AccountRow`` is
    guaranteed safe to write to Brain.
    """

    airtable_id: str
    """``_airtable_record_id`` — the Accounts table's primary key. Stable
    across name changes."""
    name: str
    """``company_name`` — the human-readable client name. Becomes the
    Drive filename (per ADR 0057 §1) and the wikilink target."""
    status: str | None
    """``status`` column. e.g., ``active`` / ``inactive`` / ``prospect``.
    Flipped to ``archived`` when the row disappears from Airtable."""
    industry: str | None
    """``industry_ai`` — denormalized industry string for prose."""
    account_manager: str | None
    """``account_manager`` — email of the owner, post-extraction (per
    ADR 0019 ``_extract: email`` annotation on singleCollaborator)."""
    drive_folder_url: str | None
    """``google_drive_folder`` — link to the client's folder in
    Solutions Drive. May be empty for non-client accounts."""
    notes: str | None
    """Freeform notes column. NOT written to body (would clobber user
    prose); surfaced in frontmatter as a single-line summary."""


@dataclass(frozen=True)
class ContactRow:
    """One row from ``airtable_replica.contacts``. ADR 0057 §3.

    HIPAA-flagged rows are excluded at query time. The HIPAA cascade —
    "Contact's primary Account is HIPAA → skip the contact too" — is
    enforced in the SQL JOIN.
    """

    airtable_id: str
    name: str
    """``name`` — becomes the Drive filename (per ADR 0057 §1) and the
    wikilink target."""
    email: str | None
    role: str | None
    organization: str | None
    """Derived: the first Account.company_name from ``contact.account``
    array, joined at read time. Empty for unaffiliated contacts."""
    relationship_type: str | None
    """``relationship_type`` — classmate | professor | mentor |
    client-contact | prospect | ..."""
    warmth: str | None
    """``warmth`` — hot | warm | cool | cold | new."""
    last_contact: date_cls | None
    next_followup: date_cls | None
    linkedin: str | None
    """``linkedin_url`` renamed to match frontmatter convention."""
    phone: str | None
    notes: str | None


@dataclass(frozen=True)
class AccountNote:
    """One Account → one ``Brain/05_GALAXY/01_ACCOUNTS/<name>.md`` upsert.

    ``frontmatter`` is the YAML block; ``body_skeleton`` is the markdown
    body written ONLY on first creation (ADR 0057 §4 invariant: body
    never overwritten thereafter outside ``<!-- AUTO -->`` sections by
    the Phase 2+ enricher).
    """

    filename: str
    frontmatter: dict[str, Any]
    body_skeleton: str


@dataclass(frozen=True)
class ContactNote:
    """One Contact → one ``Brain/05_GALAXY/02_CONTACTS/<name>.md`` upsert."""

    filename: str
    frontmatter: dict[str, Any]
    body_skeleton: str


@dataclass(frozen=True)
class UpsertOutcome:
    """Per-file result for audit + summary."""

    filename: str
    created: bool
    """True iff the file didn't exist on Drive and was just created with
    the body skeleton."""
    frontmatter_updated: bool
    """True iff the file existed but the frontmatter block was
    rewritten because at least one key differed."""
    skipped_unchanged: bool
    """True iff the file existed and the frontmatter exactly matched
    (idempotent no-op tick)."""
    error: str | None = None


@dataclass(frozen=True)
class SyncSummary:
    """Aggregate across one ``asb-people-sync`` Cloud Run Job execution.

    Galaxy counters are intentionally absent here — the Librarian sweep
    that picks up the .md files runs separately and emits its own
    ``galaxy_index`` audit rows.
    """

    accounts_listed: int = 0
    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    accounts_archived: int = 0
    accounts_hipaa_skipped: int = 0
    accounts_failed: int = 0
    contacts_listed: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_unchanged: int = 0
    contacts_archived: int = 0
    contacts_hipaa_skipped: int = 0
    contacts_failed: int = 0
