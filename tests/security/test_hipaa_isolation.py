"""HIPAA isolation security test (PRD §4.1 layer 2 + §6.2 acceptance).

Three claims under test:

1. **Filtering at source.** Every outbound Airtable request the orchestrator
   issues for a HIPAA-relevant table carries the canonical
   ``filterByFormula`` clause. A future commit that drops the clause must
   make this test fail.
2. **Lookup fields are codified.** The Airtable Lookup fields that drive the
   cascade (``Projects.Account HIPAA``, ``Tasks.Project HIPAA``,
   ``Contacts.Account HIPAA``, ``Contracts.Account HIPAA``) exist in
   ``airtable/schema.json`` with the right link/lookup wiring. Removing them
   would silently break the cascade because the formulas reference them by
   name.
3. **Removal within one cycle.** When an Account flips from ``HIPAA = false``
   to ``HIPAA = true`` between two sync runs, the second run's load-job
   payload for ``accounts`` no longer contains that record (and similarly
   for any project/task/contact/contract whose Lookup now resolves to true).
   With ``WRITE_TRUNCATE`` semantics, that means the row is gone from the
   replica after run 2.

Single-base architecture (ADR 0020) — Accounts is the HIPAA root, replacing
the legacy Clients table.

Acceptance doc ``docs/acceptance/ws-b-data-pipeline.md`` requires the
end-to-end live verification too — that's a manual runbook
(``docs/runbooks/hipaa_isolation_verification.md``) executed by the operator
against a real Airtable test base before merge. This unit test proves the
mechanism is wired correctly so the live test isn't the only safety net.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agency_brain.sync import airtable_to_bq
from agency_brain.sync.airtable_client import AirtableClient
from agency_brain.sync.hipaa_filters import HIPAA_FILTERS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_JSON = REPO_ROOT / "airtable" / "schema.json"


# ---------------------------------------------------------------------------
# Claim 2 — Lookup fields are codified
# ---------------------------------------------------------------------------


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_JSON.read_text())


def test_projects_has_account_hipaa_lookup():
    """ADR 0020: Projects.Client HIPAA → Projects.Account HIPAA after collapse."""
    schema = _load_schema()
    fields = {f["name"]: f for f in schema["tables"]["Projects"]["fields"]}
    assert (
        "Account HIPAA" in fields
    ), "Projects.Account HIPAA must exist for the source-query HIPAA cascade"
    f = fields["Account HIPAA"]
    assert f["type"] == "multipleLookupValues"
    assert f["lookup_from_link"] == "Account"
    assert f["lookup_field"] == "HIPAA"
    assert f["required"] is True


def test_tasks_has_project_hipaa_lookup():
    """Tasks.Project HIPAA chains via Project.Account HIPAA (transitive)."""
    schema = _load_schema()
    fields = {f["name"]: f for f in schema["tables"]["Tasks"]["fields"]}
    assert (
        "Project HIPAA" in fields
    ), "Tasks.Project HIPAA must exist for the transitive HIPAA cascade"
    f = fields["Project HIPAA"]
    assert f["type"] == "multipleLookupValues"
    assert f["lookup_from_link"] == "Project"
    # Post-ADR-0020: lookup target is Project's "Account HIPAA" lookup.
    assert f["lookup_field"] == "Account HIPAA"
    assert f["required"] is True


def test_contacts_and_contracts_have_account_hipaa_lookup():
    """ADR 0020: Contacts and Contracts inherit HIPAA from Accounts."""
    schema = _load_schema()
    for table_name in ("Contacts", "Contracts"):
        fields = {f["name"]: f for f in schema["tables"][table_name]["fields"]}
        assert (
            "Account HIPAA" in fields
        ), f"{table_name}.Account HIPAA must exist for the HIPAA cascade"
        f = fields["Account HIPAA"]
        assert f["type"] == "multipleLookupValues"
        assert f["lookup_from_link"] == "Account"
        assert f["lookup_field"] == "HIPAA"
        assert f["required"] is True


def test_tasks_project_link_is_required():
    """Project link must be required so the Lookup is always populated.

    A Task with no Project would have an empty ``{Project HIPAA}``; Airtable
    formulas treat empty arrays as falsy, so the row would slip through the
    filter.
    """
    schema = _load_schema()
    project_field = next(f for f in schema["tables"]["Tasks"]["fields"] if f["name"] == "Project")
    assert project_field.get("required") is True


# ---------------------------------------------------------------------------
# Claim 1 — Filtering at source (filterByFormula on every request)
# ---------------------------------------------------------------------------


class _FakeAirtableResponse:
    def __init__(self, records: list[dict[str, Any]], offset: str | None = None) -> None:
        self.status_code = 200
        self._payload = {"records": records}
        if offset:
            self._payload["offset"] = offset
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAirtableSession:
    """Records every outgoing GET so the test can assert on the params."""

    def __init__(self, table_responses: dict[str, list[dict[str, Any]]]) -> None:
        self.headers: dict[str, str] = {}
        self._table_responses = table_responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: int) -> _FakeAirtableResponse:
        # The table name is the last path segment.
        table = url.rsplit("/", 1)[-1].replace("%20", " ")
        self.calls.append({"url": url, "table": table, "params": dict(params)})
        return _FakeAirtableResponse(self._table_responses.get(table, []))


@pytest.mark.parametrize(
    "airtable_table,expected_clause",
    [
        ("Accounts", "NOT({HIPAA})"),
        ("Contacts", "NOT({Account HIPAA} = TRUE())"),
        ("Contracts", "NOT({Account HIPAA} = TRUE())"),
        ("Projects", "NOT({Account HIPAA} = TRUE())"),
        ("Tasks", "NOT({Project HIPAA} = TRUE())"),
    ],
)
def test_every_request_for_hipaa_relevant_table_carries_filter(
    airtable_table: str, expected_clause: str
):
    """Direct AirtableClient assertion — no orchestrator wiring required."""
    session = _FakeAirtableSession({airtable_table: []})
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    list(client.list_records(airtable_table, filter_formula=expected_clause))

    assert len(session.calls) == 1
    assert session.calls[0]["params"]["filterByFormula"] == expected_clause


def test_hipaa_filters_match_codified_lookup_field_names():
    """HIPAA_FILTERS strings must reference the exact Lookup field names.

    If someone renames ``Projects.Account HIPAA`` in schema.json without
    updating ``hipaa_filters.HIPAA_FILTERS``, every Projects sync silently
    pulls zero rows (or worse, fails to filter). Cross-validate names here.
    """
    schema = _load_schema()
    projects_lookup_names = {
        f["name"]
        for f in schema["tables"]["Projects"]["fields"]
        if f["type"] == "multipleLookupValues"
    }
    tasks_lookup_names = {
        f["name"]
        for f in schema["tables"]["Tasks"]["fields"]
        if f["type"] == "multipleLookupValues"
    }
    contacts_lookup_names = {
        f["name"]
        for f in schema["tables"]["Contacts"]["fields"]
        if f["type"] == "multipleLookupValues"
    }
    contracts_lookup_names = {
        f["name"]
        for f in schema["tables"]["Contracts"]["fields"]
        if f["type"] == "multipleLookupValues"
    }
    assert "Account HIPAA" in projects_lookup_names
    assert "Account HIPAA" in contacts_lookup_names
    assert "Account HIPAA" in contracts_lookup_names
    assert "Project HIPAA" in tasks_lookup_names
    assert HIPAA_FILTERS["Accounts"] == "NOT({HIPAA} = TRUE())"
    assert HIPAA_FILTERS["Contacts"] == "NOT({Account HIPAA} = TRUE())"
    assert HIPAA_FILTERS["Contracts"] == "NOT({Account HIPAA} = TRUE())"
    assert HIPAA_FILTERS["Projects"] == "NOT({Account HIPAA} = TRUE())"
    assert HIPAA_FILTERS["Tasks"] == "NOT({Project HIPAA} = TRUE())"


# ---------------------------------------------------------------------------
# Claim 3 — Removal within one cycle (HIPAA flip removes rows from replica)
# ---------------------------------------------------------------------------


class _RecordingLoadJob:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def result(self) -> None:
        return None


class _RecordingQueryJob:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def result(self) -> list[dict[str, Any]]:
        return self.rows


class _FakeBQClient:
    """Captures load_table_from_json + query calls for assertion."""

    def __init__(self) -> None:
        self.loads: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        self._checkpoint_rows: list[dict[str, Any]] = []

    def load_table_from_json(self, rows: list[dict[str, Any]], table_ref: str, job_config: Any):
        self.loads.append(
            {
                "table_ref": table_ref,
                "rows": list(rows),
                "write_disposition": str(job_config.write_disposition),
            }
        )
        return _RecordingLoadJob()

    def query(self, sql: str, job_config: Any = None):
        self.queries.append({"sql": sql, "job_config": job_config})
        job = _RecordingQueryJob()
        if "FROM `" in sql and "_sync_checkpoints" in sql and "MERGE" not in sql:
            job.rows = list(self._checkpoint_rows)
        return job


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic: str, data: bytes):
        self.published.append((topic, data))

        class _F:
            def result(self_inner, timeout):
                return f"msg-{len(self.published)}"

        return _F()


def _make_account_record(
    record_id: str, name: str, *, hipaa: bool, segment: str = "E-commerce"
) -> dict[str, Any]:
    """Build an Accounts row matching the post-ADR-0020 schema."""
    return {
        "id": record_id,
        "createdTime": "2026-04-01T00:00:00.000Z",
        "fields": {
            "Company Name": name,
            "Segment": segment,
            "Status": "Active",
            "Account Manager": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
            "HIPAA": hipaa,
            "Last Activity Timestamp": "2026-04-25T10:30:00.000Z",
        },
    }


def _make_minimal_record(record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record_id,
        "createdTime": "2026-04-01T00:00:00.000Z",
        "fields": fields,
    }


def _build_table_responses(*, include_hipaa_account: bool) -> dict[str, list[dict[str, Any]]]:
    """Simulate Airtable's filterByFormula behavior.

    The filter is applied at the API layer — when ``include_hipaa_account`` is
    False, the response for Accounts excludes ``recHIPAA1``; the responses for
    Contacts/Contracts/Projects/Tasks exclude anything pointing at it
    (mirroring the Lookup cascade). This is exactly what Airtable does in
    production when ``NOT({HIPAA})`` / ``NOT({Account HIPAA})`` /
    ``NOT({Project HIPAA})`` are set.
    """
    accounts = [_make_account_record("recA1", "Acme Co", hipaa=False)]
    projects = [
        _make_minimal_record(
            "recP1",
            {
                "Project Name": "Acme — Website Design — Q2 2026",
                "Account": ["recA1"],
                "Service": ["recSVCWEB"],
                "Phase": "Discovery",
                "Status": "Active",
                "Owner": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
            },
        )
    ]
    tasks = [
        _make_minimal_record(
            "recT1",
            {
                "Task Name": "First task",
                "Source": "Manual",
                "Project": ["recP1"],
                "Owner": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
                "Action Type": "Do It Now",
                "Status": "Open",
                "Approval Status": "Approved",
            },
        )
    ]
    if include_hipaa_account:
        accounts.append(_make_account_record("recHIPAA1", "Hospice LLC", hipaa=True))
        projects.append(
            _make_minimal_record(
                "recPHipaa",
                {
                    "Project Name": "Hospice strategy",
                    "Account": ["recHIPAA1"],
                    "Service": ["recSVCSTRAT"],
                    "Phase": "Discovery",
                    "Status": "Active",
                    "Owner": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
                },
            )
        )
        tasks.append(
            _make_minimal_record(
                "recTHipaa",
                {
                    "Task Name": "Draft proposal",
                    "Source": "Manual",
                    "Project": ["recPHipaa"],
                    "Owner": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
                    "Action Type": "Do It Now",
                    "Status": "Open",
                    "Approval Status": "Approved",
                },
            )
        )
    return {
        "Accounts": accounts,
        "Contacts": [],
        "Contracts": [],
        "Projects": projects,
        "Tasks": tasks,
        "Team": [],
        "Goals": [],
        "Goal Scores": [],
        "Risk Profiles": [],
        "Service Catalog": [],
    }


def _run_sync(table_responses: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Run the orchestrator against fake clients; return the captured state."""
    bq = _FakeBQClient()
    pub = _RecordingPublisher()
    session = _FakeAirtableSession(table_responses)
    summary = airtable_to_bq.sync(
        airtable_base_id="appTEST",
        airtable_pat="patFAKE",
        project_id="brain-test",
        drift_topic="asb-schema-drift-alerts",
        schema_json_path=SCHEMA_JSON,
        bq_client=bq,
        pubsub_publisher=pub,
        airtable_session=session,
    )
    return {"bq": bq, "pub": pub, "session": session, "summary": summary}


def test_every_orchestrator_request_carries_hipaa_filter():
    """End-to-end claim 1: no Airtable request is missing filterByFormula."""
    state = _run_sync(_build_table_responses(include_hipaa_account=False))
    session: _FakeAirtableSession = state["session"]

    assert session.calls, "orchestrator must hit Airtable"
    by_table = {c["table"]: c for c in session.calls}
    assert by_table["Accounts"]["params"]["filterByFormula"] == "NOT({HIPAA} = TRUE())"
    assert by_table["Contacts"]["params"]["filterByFormula"] == "NOT({Account HIPAA} = TRUE())"
    assert by_table["Contracts"]["params"]["filterByFormula"] == "NOT({Account HIPAA} = TRUE())"
    assert by_table["Projects"]["params"]["filterByFormula"] == "NOT({Account HIPAA} = TRUE())"
    assert by_table["Tasks"]["params"]["filterByFormula"] == "NOT({Project HIPAA} = TRUE())"


def _record_ids_in_load(bq: _FakeBQClient, table_id: str) -> set[str]:
    for load in bq.loads:
        if load["table_ref"].endswith(f".{table_id}"):
            return {row["_airtable_record_id"] for row in load["rows"]}
    raise AssertionError(f"no load job for table {table_id}")


def test_run_with_hipaa_account_excluded_loads_no_hipaa_rows():
    """Run 1: ``recHIPAA1`` is HIPAA=true so Airtable filters it out.

    Asserts the load-job payload (the actual write to BigQuery) contains
    none of the HIPAA-flagged record IDs across accounts/projects/tasks.
    """
    state = _run_sync(_build_table_responses(include_hipaa_account=False))
    bq: _FakeBQClient = state["bq"]

    assert _record_ids_in_load(bq, "accounts") == {"recA1"}
    assert _record_ids_in_load(bq, "projects") == {"recP1"}
    assert _record_ids_in_load(bq, "tasks") == {"recT1"}


def test_hipaa_flip_removes_row_via_write_truncate():
    """Run 1: account present (HIPAA=false). Run 2: filtered (HIPAA=true).

    The second run issues a WRITE_TRUNCATE load with only ``recA1`` in the
    payload — which deletes ``recHIPAA1`` from the replica atomically.
    Same for the linked project (``recPHipaa``) and task (``recTHipaa``).
    """
    # Run 1: HIPAA=false on the test account → it appears in the response.
    run1_state = _run_sync(_build_table_responses(include_hipaa_account=True))
    run1_bq: _FakeBQClient = run1_state["bq"]
    assert "recHIPAA1" in _record_ids_in_load(run1_bq, "accounts")

    # Run 2: HIPAA=true → Airtable filterByFormula excludes the row.
    run2_state = _run_sync(_build_table_responses(include_hipaa_account=False))
    run2_bq: _FakeBQClient = run2_state["bq"]

    assert _record_ids_in_load(run2_bq, "accounts") == {"recA1"}
    assert _record_ids_in_load(run2_bq, "projects") == {"recP1"}
    assert _record_ids_in_load(run2_bq, "tasks") == {"recT1"}

    # Verify the replacement is via WRITE_TRUNCATE (not append) — which is
    # what guarantees row removal in one cycle.
    for load in run2_bq.loads:
        assert "WRITE_TRUNCATE" in load["write_disposition"]


def test_no_drift_event_published_for_known_columns():
    """Drift surfaces only on schema additions, not every run."""
    state = _run_sync(_build_table_responses(include_hipaa_account=False))
    pub: _RecordingPublisher = state["pub"]
    assert pub.published == []


def test_drift_event_published_for_unknown_column():
    """A column appearing in the API response that's not in schema.json must
    produce a Pub/Sub message. Asserts PRD §6.2 surfacing requirement.
    """
    table_responses = _build_table_responses(include_hipaa_account=False)
    table_responses["Accounts"][0]["fields"]["Mystery New Column"] = "surprise"
    state = _run_sync(table_responses)
    pub: _RecordingPublisher = state["pub"]

    drift_msgs = [json.loads(body) for _, body in pub.published]
    assert any(
        m["table"] == "Accounts" and "Mystery New Column" in m["new_columns"] for m in drift_msgs
    ), "unknown column must produce a asb-schema-drift-alerts message"


def test_checkpoint_row_written_per_table():
    """Each successfully synced table writes a MERGE to ``_sync_checkpoints``.

    The orchestrator does not write the checkpoint when the load job fails;
    this test covers the happy path. Failure semantics live in
    ``test_orchestrator_per_table_isolation``.
    """
    state = _run_sync(_build_table_responses(include_hipaa_account=False))
    bq: _FakeBQClient = state["bq"]
    merges = [q for q in bq.queries if "MERGE" in q["sql"]]
    # One MERGE per synced table (12 tables in schema.json: 10 from
    # ADR 0020 + Captures per ADR 0039 + Orchestrator Inbox per audit F7).
    assert len(merges) == 12


def test_record_translation_strips_lookup_fields():
    """Lookup field values returned by Airtable must NOT land in BQ rows.

    The replica schema doesn't include them; if the translator forgot to
    skip them, the load job would fail with "no such field" or worse,
    silently store HIPAA values in an aspect-tagged column.
    """
    schema = _load_schema()
    bq_schemas = airtable_to_bq.replica_table_schemas(SCHEMA_JSON)
    record = {
        "id": "recP1",
        "createdTime": "2026-04-01T00:00:00.000Z",
        "fields": {
            "Project Name": "Acme — Website Design — Q2 2026",
            "Account": ["recA1"],
            "Service": ["recSVCWEB"],
            "Phase": "Discovery",
            "Status": "Active",
            "Owner": {"id": "usrAM1", "email": "am1@example.com", "name": "AM 1"},
            "Account HIPAA": [False],  # ← Lookup value present in API response
        },
    }
    row = airtable_to_bq.airtable_record_to_bq_row(
        record=record,
        table_name="Projects",
        table_def=schema["tables"]["Projects"],
        bq_schema=bq_schemas["projects"],
        sync_run_id="run-1",
        synced_at=datetime(2026, 4, 25, tzinfo=UTC),
    )
    assert "account_hipaa" not in row, "Lookup-field values must be dropped"
    assert row["_airtable_record_id"] == "recP1"
    assert row["hipaa_excluded"] is False
