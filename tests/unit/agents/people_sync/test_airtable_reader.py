"""Tests for ``people_sync.airtable_reader`` — BQ adapter against fakes."""

from __future__ import annotations

from datetime import date

from agency_brain.agents.people_sync.airtable_reader import (
    AirtableReader,
)


class _FakeBQ:
    """Records queries, returns canned responses keyed on a substring match."""

    def __init__(self, *, responses: dict[str, list[dict]] | None = None) -> None:
        self._responses = responses or {}
        self.queries: list[str] = []

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.queries.append(sql)
        # Return whichever canned response's key is a substring of the SQL.
        for key, rows in self._responses.items():
            if key in sql:
                return list(rows)
        return []


def test_list_accounts_maps_columns_to_dataclass() -> None:
    bq = _FakeBQ(
        responses={
            "FROM `p.airtable_replica.accounts`": [
                {
                    "airtable_id": "recAAA",
                    "name": "Client A",
                    "status": "Active",
                    "industry": "Investigations",
                    "account_manager": "owner@example.com",
                    "drive_folder_url": "https://drive.google.com/...",
                    "notes": "Long-term client",
                }
            ]
        }
    )
    reader = AirtableReader(bq_query=bq, project_id="p")
    accounts = reader.list_accounts()
    assert len(accounts) == 1
    a = accounts[0]
    assert a.airtable_id == "recAAA"
    assert a.name == "Client A"
    assert a.status == "Active"
    assert a.industry == "Investigations"
    assert a.account_manager == "owner@example.com"
    assert a.drive_folder_url == "https://drive.google.com/..."
    assert a.notes == "Long-term client"


def test_list_accounts_strips_blank_strings_to_none() -> None:
    bq = _FakeBQ(
        responses={
            "FROM `p.airtable_replica.accounts`": [
                {
                    "airtable_id": "recBBB",
                    "name": "Tiny Client",
                    "status": None,
                    "industry": "",
                    "account_manager": "  ",
                    "drive_folder_url": None,
                    "notes": None,
                }
            ]
        }
    )
    reader = AirtableReader(bq_query=bq, project_id="p")
    accounts = reader.list_accounts()
    a = accounts[0]
    assert a.status is None
    assert a.industry is None
    assert a.account_manager is None
    assert a.drive_folder_url is None
    assert a.notes is None


def test_list_contacts_maps_columns_including_dates_and_organization() -> None:
    bq = _FakeBQ(
        responses={
            "FROM contacts_with_primary": [
                {
                    "airtable_id": "recCCC",
                    "name": "Sam Q",
                    "email": "sam@example.com",
                    "role": "Director of Ops",
                    "organization": "Client A",
                    "relationship_type": "client-contact",
                    "warmth": "warm",
                    "last_contact": "2026-03-28",
                    "next_followup": "2026-04-15",
                    "linkedin": "https://linkedin.com/in/sam",
                    "phone": "555-0100",
                    "notes": "Prefers email over phone",
                }
            ]
        }
    )
    reader = AirtableReader(bq_query=bq, project_id="p")
    contacts = reader.list_contacts()
    assert len(contacts) == 1
    c = contacts[0]
    assert c.airtable_id == "recCCC"
    assert c.name == "Sam Q"
    assert c.email == "sam@example.com"
    assert c.organization == "Client A"
    assert c.warmth == "warm"
    assert c.last_contact == date(2026, 3, 28)
    assert c.next_followup == date(2026, 4, 15)
    assert c.linkedin == "https://linkedin.com/in/sam"


def test_list_contacts_tolerates_bad_date_string() -> None:
    bq = _FakeBQ(
        responses={
            "FROM contacts_with_primary": [
                {
                    "airtable_id": "recDDD",
                    "name": "Bad Date",
                    "email": "x@y.com",
                    "role": None,
                    "organization": None,
                    "relationship_type": "classmate",
                    "warmth": "new",
                    "last_contact": "not-a-date",
                    "next_followup": None,
                    "linkedin": None,
                    "phone": None,
                    "notes": None,
                }
            ]
        }
    )
    reader = AirtableReader(bq_query=bq, project_id="p")
    contacts = reader.list_contacts()
    assert contacts[0].last_contact is None
    assert contacts[0].next_followup is None


def test_accounts_sql_filters_hipaa_and_excludes() -> None:
    """The accounts query must exclude hipaa=true AND hipaa_excluded=true rows."""
    bq = _FakeBQ()
    reader = AirtableReader(bq_query=bq, project_id="agency-brain-demo")
    reader.list_accounts()
    sql = bq.queries[0]
    assert "hipaa, FALSE) = FALSE" in sql
    assert "hipaa_excluded, FALSE) = FALSE" in sql


def test_contacts_sql_enforces_hipaa_cascade() -> None:
    """Contacts whose primary account is HIPAA must be excluded via the LEFT JOIN.

    The cascade pattern: contacts with non-NULL primary_account_id that
    DON'T appear in non_hipaa_accounts (because their account was
    filtered by the CTE's hipaa = false predicate) get acc.account_id =
    NULL after the LEFT JOIN, and the WHERE clause's
    ``primary_account_id IS NULL OR acc.account_id IS NOT NULL`` then
    excludes them.
    """
    bq = _FakeBQ()
    reader = AirtableReader(bq_query=bq, project_id="p")
    reader.list_contacts()
    sql = bq.queries[0]
    assert "non_hipaa_accounts" in sql
    assert "LEFT JOIN non_hipaa_accounts" in sql
    assert "primary_account_id IS NULL" in sql
    assert "acc.account_id IS NOT NULL" in sql
