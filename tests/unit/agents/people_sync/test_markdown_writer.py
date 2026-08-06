"""Tests for ``people_sync.markdown_writer`` — pure composition."""

from __future__ import annotations

from datetime import date

from agency_brain.agents.people_sync.markdown_writer import (
    make_account_note,
    make_archived_frontmatter_update,
    make_contact_note,
)
from agency_brain.agents.people_sync.models import AccountRow, ContactRow


def _account(**overrides) -> AccountRow:
    base = dict(
        airtable_id="recAAA",
        name="Client A",
        status="Active",
        industry="Investigations",
        account_manager="owner@example.com",
        drive_folder_url="https://drive.google.com/folders/...",
        notes="Long-term client",
    )
    base.update(overrides)
    return AccountRow(**base)


def _contact(**overrides) -> ContactRow:
    base = dict(
        airtable_id="recCCC",
        name="Sam Q",
        email="sam@example.com",
        role="Director of Ops",
        organization="Client A",
        relationship_type="client-contact",
        warmth="warm",
        last_contact=date(2026, 3, 28),
        next_followup=date(2026, 4, 15),
        linkedin="https://linkedin.com/in/sam",
        phone="555-0100",
        notes=None,
    )
    base.update(overrides)
    return ContactRow(**base)


def test_make_account_note_filename_preserves_name_verbatim() -> None:
    note = make_account_note(_account())
    assert note.filename == "Client A.md"


def test_make_account_note_strips_drive_illegal_chars_and_collapses_whitespace() -> None:
    # / and : and ? get stripped; the runs of whitespace that result
    # collapse to single spaces.
    note = make_account_note(_account(name="Acme / Foo: Bar?"))
    assert note.filename == "Acme Foo Bar.md"


def test_make_account_note_frontmatter_status_lowercased() -> None:
    note = make_account_note(_account(status="Active"))
    assert note.frontmatter["status"] == "active"


def test_make_account_note_frontmatter_hipaa_false() -> None:
    """HIPAA is filtered upstream — once a row reaches the writer, hipaa is always False."""
    note = make_account_note(_account())
    assert note.frontmatter["hipaa"] is False


def test_make_account_note_relationship_type_always_client() -> None:
    note = make_account_note(_account())
    assert note.frontmatter["relationship_type"] == "client"


def test_make_account_note_body_skeleton_has_auto_sections() -> None:
    note = make_account_note(_account())
    assert "## Active engagements" in note.body_skeleton
    assert "## Recent activity" in note.body_skeleton
    assert "## Open risks" in note.body_skeleton
    assert "AUTO: populated by asb-people-sync" in note.body_skeleton
    # And the user-prose sections (no AUTO marker on these)
    assert "## Who they are" in note.body_skeleton
    assert "## Connections" in note.body_skeleton


def test_make_contact_note_frontmatter_serializes_dates() -> None:
    note = make_contact_note(_contact())
    assert note.frontmatter["last_contact"] == "2026-03-28"
    assert note.frontmatter["next_followup"] == "2026-04-15"


def test_make_contact_note_warmth_lowercased() -> None:
    note = make_contact_note(_contact(warmth="WARM"))
    assert note.frontmatter["warmth"] == "warm"


def test_make_contact_note_empty_optional_fields_become_empty_string() -> None:
    note = make_contact_note(_contact(email=None, phone=None, linkedin=None))
    assert note.frontmatter["email"] == ""
    assert note.frontmatter["phone"] == ""
    assert note.frontmatter["linkedin"] == ""


def test_make_contact_note_filename_collisions_not_auto_resolved() -> None:
    """Filename collisions are resolved in drive_writer (where we can
    detect they exist for a different airtable_id), not here. The
    markdown_writer just emits the natural filename."""
    a = make_contact_note(_contact(name="Sam Q", airtable_id="rec1"))
    b = make_contact_note(_contact(name="Sam Q", airtable_id="rec2"))
    assert a.filename == b.filename == "Sam Q.md"


def test_make_archived_frontmatter_preserves_existing_keys() -> None:
    existing = {
        "type": "person",
        "airtable_id": "recDEL",
        "name": "Gone Contact",
        "warmth": "cold",
        "synced_at": "2026-05-17T15:00:00Z",
    }
    archived = make_archived_frontmatter_update(existing)
    assert archived["status"] == "archived"
    assert archived["airtable_id"] == "recDEL"
    assert archived["name"] == "Gone Contact"
    assert archived["warmth"] == "cold"  # Preserved
    assert "archived_at" in archived
    assert "archived_reason" in archived
