"""Unit tests for person.{person_summary, sync_people} MCP tools (ADR 0057)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from agency_brain.mcp_server.tools import person as person_tools


class _FakeBQ:
    def __init__(self, fixtures: list[tuple[str, list[dict]]] | None = None) -> None:
        self._fixtures = fixtures or []
        self.calls: list[tuple[str, list[dict]]] = []

    def __call__(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.calls.append((sql, parameters or []))
        for substr, rows in self._fixtures:
            if substr in sql:
                return rows
        return []


class _FakeConfig:
    project_id = "p"


@pytest.fixture
def fake_bq(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeBQ()
    monkeypatch.setattr(person_tools, "query_rows", fake)
    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())
    return fake


# --------------------------------------------------------------- person_summary


def test_person_summary_returns_found_false_when_no_match(fake_bq) -> None:
    fake_bq._fixtures = []
    out = person_tools.person_summary(name_or_email="Nobody")
    assert out == {"found": False}


def test_person_summary_returns_found_false_for_empty_query(fake_bq) -> None:
    assert person_tools.person_summary(name_or_email="") == {"found": False}
    assert person_tools.person_summary(name_or_email="   ") == {"found": False}


def test_person_summary_resolves_by_name_and_pulls_organization(fake_bq) -> None:
    today = date.today()
    fake_bq._fixtures = [
        (
            "airtable_replica.contacts",
            [
                {
                    "airtable_id": "recC1",
                    "name": "Sam Q",
                    "email": "sam@example.com",
                    "role": "Director",
                    "relationship_type": "client-contact",
                    "warmth": "warm",
                    "last_contact": date(2026, 3, 28),
                    "next_followup": today - timedelta(days=1),
                    "linkedin": "https://linkedin.com/in/sam",
                    "phone": "555-0100",
                    "notes": "Prefers email",
                    "primary_account_id": "recA1",
                }
            ],
        ),
        ("airtable_replica.accounts", [{"company_name": "Client A"}]),
        (
            "agent_outputs.triaged_items",
            [
                {
                    "activity_date": "2026-05-18",
                    "line": "triage: Re: Q3 brief",
                    "source_detail": "sam@example.com",
                },
                {
                    "activity_date": "2026-05-12",
                    "line": "triage: schedule check",
                    "source_detail": "sam@example.com",
                },
            ],
        ),
    ]
    out = person_tools.person_summary(name_or_email="Sam Q")
    assert out["found"] is True
    assert out["name"] == "Sam Q"
    assert out["email"] == "sam@example.com"
    assert out["organization"] == "Client A"
    assert out["warmth"] == "warm"
    assert out["last_contact"] == "2026-03-28"
    assert out["open_followup_due"] is True  # next_followup is yesterday
    assert len(out["recent_activity"]) == 2
    assert out["recent_activity"][0]["line"] == "triage: Re: Q3 brief"


def test_person_summary_no_account_skips_organization_query(fake_bq) -> None:
    fake_bq._fixtures = [
        (
            "airtable_replica.contacts",
            [
                {
                    "airtable_id": "recC2",
                    "name": "Solo Person",
                    "email": "solo@example.com",
                    "role": None,
                    "relationship_type": "classmate",
                    "warmth": "new",
                    "last_contact": None,
                    "next_followup": None,
                    "linkedin": None,
                    "phone": None,
                    "notes": None,
                    "primary_account_id": None,
                }
            ],
        ),
        ("agent_outputs.triaged_items", []),
    ]
    out = person_tools.person_summary(name_or_email="Solo Person")
    assert out["found"] is True
    assert out["organization"] is None
    # accounts query should NOT have fired (only contacts + triaged_items)
    queries = [sql for sql, _ in fake_bq.calls]
    assert not any("airtable_replica.accounts" in q for q in queries)


def test_person_summary_open_followup_false_when_future(fake_bq) -> None:
    future = date.today() + timedelta(days=7)
    fake_bq._fixtures = [
        (
            "airtable_replica.contacts",
            [
                {
                    "airtable_id": "recC3",
                    "name": "Future Followup",
                    "email": "ff@example.com",
                    "role": None,
                    "relationship_type": "mentor",
                    "warmth": "warm",
                    "last_contact": None,
                    "next_followup": future,
                    "linkedin": None,
                    "phone": None,
                    "notes": None,
                    "primary_account_id": None,
                }
            ],
        ),
    ]
    out = person_tools.person_summary(name_or_email="Future Followup")
    assert out["open_followup_due"] is False


def test_person_summary_passes_hipaa_exclude_through_sql(fake_bq) -> None:
    person_tools.person_summary(name_or_email="x")
    sql, _ = fake_bq.calls[0]
    assert "hipaa_excluded, FALSE) = FALSE" in sql


# --------------------------------------------------------------- sync_people


def test_sync_people_returns_failure_when_gcloud_not_found(monkeypatch) -> None:
    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())

    def _boom(*args, **kwargs):
        raise FileNotFoundError("no gcloud")

    monkeypatch.setattr(person_tools.subprocess, "run", _boom)
    out = person_tools.sync_people()
    assert out["succeeded"] is False
    assert "gcloud not on PATH" in out["stderr_tail"]


def test_sync_people_returns_failure_on_timeout(monkeypatch) -> None:
    import subprocess as _sp

    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())

    def _timeout(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd="gcloud", timeout=900)

    monkeypatch.setattr(person_tools.subprocess, "run", _timeout)
    out = person_tools.sync_people()
    assert out["succeeded"] is False
    assert "timeout" in out["stderr_tail"]


def test_sync_people_returns_success_when_gcloud_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())

    class _Proc:
        returncode = 0
        stdout = "asb-people-sync-abc123\n"
        stderr = ""

    monkeypatch.setattr(person_tools.subprocess, "run", lambda *a, **kw: _Proc())
    out = person_tools.sync_people()
    assert out["succeeded"] is True
    assert "asb-people-sync-abc123" in out["execution_name"]


def test_sync_people_rejects_region_outside_allowlist(monkeypatch) -> None:
    """region is an LLM-callable arg into a gcloud argv; reject anything
    not on the allowlist before subprocess runs."""
    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())
    ran = {"called": False}

    def _spy(*a, **kw):
        ran["called"] = True
        raise AssertionError("subprocess must not run for a bad region")

    monkeypatch.setattr(person_tools.subprocess, "run", _spy)
    out = person_tools.sync_people(region="us-east1 --update-env-vars=X=Y")
    assert out["succeeded"] is False
    assert "invalid region" in out["stderr_tail"]
    assert ran["called"] is False


def test_sync_people_returns_failure_when_gcloud_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(person_tools, "get_config", lambda: _FakeConfig())

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (gcloud.run.jobs.execute) PERMISSION_DENIED"

    monkeypatch.setattr(person_tools.subprocess, "run", lambda *a, **kw: _Proc())
    out = person_tools.sync_people()
    assert out["succeeded"] is False
    assert out["execution_name"] is None
    assert "PERMISSION_DENIED" in out["stderr_tail"]


# --------------------------------------------------------------- pending_followups


def test_pending_followups_default_window_is_today_or_overdue(fake_bq) -> None:
    fake_bq._fixtures = [
        (
            "airtable_replica.contacts",
            [
                {
                    "contact_id": "recA",
                    "name": "Sam Q",
                    "email": "sam@example.com",
                    "warmth": "warm",
                    "relationship_type": "classmate",
                    "last_contact": date(2026, 4, 15),
                    "next_followup": date(2026, 5, 10),
                    "primary_account_id": None,
                },
                {
                    "contact_id": "recB",
                    "name": "Jamie K",
                    "email": "jamie@example.com",
                    "warmth": "hot",
                    "relationship_type": "client",
                    "last_contact": date(2026, 5, 1),
                    "next_followup": date(2026, 5, 19),
                    "primary_account_id": "recAcct1",
                },
            ],
        )
    ]
    out = person_tools.pending_followups()
    assert out["total"] == 2
    assert out["window_days"] == 0
    assert [c["name"] for c in out["contacts"]] == ["Sam Q", "Jamie K"]
    assert out["contacts"][0]["next_followup"] == "2026-05-10"
    assert out["contacts"][1]["primary_account_id"] == "recAcct1"
    sql, params = fake_bq.calls[0]
    assert "c.next_followup IS NOT NULL" in sql
    assert "DATE_ADD(CURRENT_DATE(), INTERVAL @window_days DAY)" in sql
    assert "c.hipaa_excluded" in sql
    assert "ORDER BY c.next_followup ASC" in sql
    by_name = {p["name"]: p["value"] for p in params}
    assert by_name["window_days"] == 0
    assert by_name["limit"] == 20


def test_pending_followups_window_clamped(fake_bq) -> None:
    person_tools.pending_followups(window_days=120, limit=500)
    _, params = fake_bq.calls[0]
    by_name = {p["name"]: p["value"] for p in params}
    assert by_name["window_days"] == 60
    assert by_name["limit"] == 100

    person_tools.pending_followups(window_days=-5, limit=0)
    _, params = fake_bq.calls[1]
    by_name = {p["name"]: p["value"] for p in params}
    assert by_name["window_days"] == 0
    assert by_name["limit"] == 1


def test_pending_followups_empty_returns_empty_contacts(fake_bq) -> None:
    out = person_tools.pending_followups()
    assert out == {"contacts": [], "total": 0, "window_days": 0}


def test_pending_followups_handles_null_optional_fields(fake_bq) -> None:
    fake_bq._fixtures = [
        (
            "airtable_replica.contacts",
            [
                {
                    "contact_id": "recC",
                    "name": "Pat L",
                    "email": None,
                    "warmth": None,
                    "relationship_type": None,
                    "last_contact": None,
                    "next_followup": date(2026, 5, 19),
                    "primary_account_id": None,
                }
            ],
        )
    ]
    out = person_tools.pending_followups()
    c = out["contacts"][0]
    assert c["email"] is None
    assert c["last_contact"] is None
    assert c["next_followup"] == "2026-05-19"
