"""Integration test for the People Sync sync routines.

We don't exercise main() end-to-end (that pulls in google.cloud.bigquery
+ ADCDriveServiceFactory which need GCP credentials). Instead we test
the inner _sync_accounts / _sync_contacts helpers against fakes.
"""

from __future__ import annotations

from datetime import date

from agency_brain.agents.people_sync.main import (
    _sync_accounts,
    _sync_contacts,
)
from agency_brain.agents.people_sync.markdown_writer import (
    make_account_note,
    make_contact_note,
)
from agency_brain.agents.people_sync.models import (
    AccountRow,
    ContactRow,
    SyncSummary,
    UpsertOutcome,
)

# --------------------------------------------------------------- fakes


class _FakeReader:
    def __init__(self, *, accounts=None, contacts=None) -> None:
        self._accounts = list(accounts or [])
        self._contacts = list(contacts or [])

    def list_accounts(self):
        return list(self._accounts)

    def list_contacts(self):
        return list(self._contacts)


class _FakeWriter:
    def __init__(self, *, existing=None, upsert_returns=None, archive_returns=None) -> None:
        # existing: dict[airtable_id, ExistingFile]
        self._existing = existing or {}
        self.upsert_calls: list[dict] = []
        self.archive_calls: list[str] = []
        self._upsert_returns = upsert_returns or {}
        self._archive_returns = archive_returns or {}

    def list_existing(self):
        return dict(self._existing)

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)
        # Default: created
        return self._upsert_returns.get(
            kwargs["airtable_id"],
            UpsertOutcome(
                filename=kwargs["filename"],
                created=True,
                frontmatter_updated=False,
                skipped_unchanged=False,
            ),
        )

    def mark_archived(self, *, airtable_id, reason="deleted from Airtable"):
        self.archive_calls.append(airtable_id)
        return self._archive_returns.get(airtable_id)


# --------------------------------------------------------------- tests


def test_sync_accounts_creates_new_files() -> None:
    rows = [
        AccountRow(
            airtable_id="recA1",
            name="Client A",
            status="Active",
            industry="Investigations",
            account_manager="owner@...",
            drive_folder_url=None,
            notes=None,
        ),
        AccountRow(
            airtable_id="recA2",
            name="Client C Studio",
            status="Active",
            industry="Design",
            account_manager="owner@...",
            drive_folder_url=None,
            notes=None,
        ),
    ]
    reader = _FakeReader(accounts=rows)
    writer = _FakeWriter()
    summary = _sync_accounts(
        reader=reader,
        writer=writer,
        summary=SyncSummary(),
        make_note=make_account_note,
    )
    assert summary.accounts_listed == 2
    assert summary.accounts_created == 2
    assert summary.accounts_updated == 0
    assert summary.accounts_archived == 0
    assert len(writer.upsert_calls) == 2


def test_sync_contacts_handles_mixed_outcomes() -> None:
    rows = [
        ContactRow(
            airtable_id="recC1",
            name="Sam Q",
            email="sam@...",
            role="Director",
            organization="Client A",
            relationship_type="client-contact",
            warmth="warm",
            last_contact=date(2026, 3, 28),
            next_followup=date(2026, 4, 15),
            linkedin=None,
            phone=None,
            notes=None,
        ),
        ContactRow(
            airtable_id="recC2",
            name="Pat Existing",
            email="pat@...",
            role=None,
            organization=None,
            relationship_type="classmate",
            warmth="warm",
            last_contact=None,
            next_followup=None,
            linkedin=None,
            phone=None,
            notes=None,
        ),
        ContactRow(
            airtable_id="recC3",
            name="Unchanged",
            email="u@...",
            role=None,
            organization=None,
            relationship_type="classmate",
            warmth="warm",
            last_contact=None,
            next_followup=None,
            linkedin=None,
            phone=None,
            notes=None,
        ),
    ]
    reader = _FakeReader(contacts=rows)
    writer = _FakeWriter(
        upsert_returns={
            "recC1": UpsertOutcome(
                filename="Sam Q.md",
                created=True,
                frontmatter_updated=False,
                skipped_unchanged=False,
            ),
            "recC2": UpsertOutcome(
                filename="Pat Existing.md",
                created=False,
                frontmatter_updated=True,
                skipped_unchanged=False,
            ),
            "recC3": UpsertOutcome(
                filename="Unchanged.md",
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=True,
            ),
        }
    )
    summary = _sync_contacts(
        reader=reader,
        writer=writer,
        summary=SyncSummary(),
        make_note=make_contact_note,
    )
    assert summary.contacts_listed == 3
    assert summary.contacts_created == 1
    assert summary.contacts_updated == 1
    assert summary.contacts_unchanged == 1
    assert summary.contacts_failed == 0


def test_sync_archives_rows_no_longer_in_airtable() -> None:
    """ADR 0057 §4: Drive files whose airtable_id vanished from the
    incoming row set get flipped to status: archived."""
    from agency_brain.agents.people_sync.drive_writer import ExistingFile

    rows = [
        AccountRow(
            airtable_id="recA1",
            name="Still Active",
            status="Active",
            industry=None,
            account_manager=None,
            drive_folder_url=None,
            notes=None,
        ),
    ]
    existing = {
        "recA1": ExistingFile(
            file_id="f1", name="Still Active.md", airtable_id="recA1", raw_content=""
        ),
        "recGONE": ExistingFile(
            file_id="f2", name="Gone Client.md", airtable_id="recGONE", raw_content=""
        ),
    }
    writer = _FakeWriter(
        existing=existing,
        archive_returns={
            "recGONE": UpsertOutcome(
                filename="Gone Client.md",
                created=False,
                frontmatter_updated=True,
                skipped_unchanged=False,
            ),
        },
    )
    summary = _sync_accounts(
        reader=_FakeReader(accounts=rows),
        writer=writer,
        summary=SyncSummary(),
        make_note=make_account_note,
    )
    assert writer.archive_calls == ["recGONE"]
    assert summary.accounts_archived == 1


def test_sync_counts_failed_upserts() -> None:
    rows = [
        AccountRow(
            airtable_id="recA1",
            name="Boom",
            status="Active",
            industry=None,
            account_manager=None,
            drive_folder_url=None,
            notes=None,
        ),
    ]
    writer = _FakeWriter(
        upsert_returns={
            "recA1": UpsertOutcome(
                filename="Boom.md",
                created=False,
                frontmatter_updated=False,
                skipped_unchanged=False,
                error="DriveWriteError: 403",
            ),
        },
    )
    summary = _sync_accounts(
        reader=_FakeReader(accounts=rows),
        writer=writer,
        summary=SyncSummary(),
        make_note=make_account_note,
    )
    assert summary.accounts_failed == 1
    assert summary.accounts_created == 0
