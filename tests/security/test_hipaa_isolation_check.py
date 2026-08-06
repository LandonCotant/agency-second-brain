"""Integration test for the hourly HIPAA isolation runtime check.

The script runs as a Cloud Run Job; this test exercises the same code path
the Cloud Run Job does (``run_check`` + ``main``) with a fake BigQuery
client so the assertion is "the right audit row + structured stdout came
out for each input shape."

Mirrors the fixture style of ``tests/security/test_hipaa_isolation.py``
(_FakeBQClient, _FakeAirtableSession, etc.) — same project conventions, no
live GCP calls.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from agency_brain.audit import hipaa_isolation_check
from agency_brain.audit._reporter import (
    AuditEventId,
    AuditScriptContext,
    DriftReporter,
)
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.models import HipaaGuardStatus

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Row(dict):
    """Stand-in for a google.cloud.bigquery.Row that supports dict() conversion."""


class _FakeQueryJob:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def result(self) -> list[_Row]:
        return self._rows


_LEADING_FROM_RE = __import__("re").compile(
    r"FROM\s+`[^`]+\.([a-z_]+\.[a-z_]+)`", __import__("re").IGNORECASE
)


class _FakeBQClient:
    """Returns scripted query results based on the FIRST table in the FROM clause.

    Matching the leading FROM (rather than any substring) avoids the
    pitfall where ``airtable_replica.clients`` appears as a JOIN target in
    the projects/tasks/triaged/risk queries — those queries' leading FROM
    is their primary table, which is what we want to key on.
    """

    def __init__(self, table_to_rows: dict[str, list[_Row]]) -> None:
        self._table_to_rows = table_to_rows
        self.queries: list[str] = []

    def query(self, sql: str) -> _FakeQueryJob:
        self.queries.append(sql)
        m = _LEADING_FROM_RE.search(sql)
        if m and m.group(1) in self._table_to_rows:
            return _FakeQueryJob(self._table_to_rows[m.group(1)])
        return _FakeQueryJob([])


class _RaisingBQClient:
    """Raises on the first query — exercises the SECURITY_AUDIT_ERROR branch."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.queries: list[str] = []

    def query(self, sql: str) -> _FakeQueryJob:
        self.queries.append(sql)
        raise self._exc


class _RecordingAuditLog(AuditLogClient):
    """AuditLogClient subclass that records emitted events instead of writing to BQ."""

    def __init__(self) -> None:
        super().__init__(project_id="agency-brain-demo")
        self.emitted = []

    def emit(self, event):  # type: ignore[override]
        self.emitted.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_reporter(audit_log: AuditLogClient) -> DriftReporter:
    """A DriftReporter wired to a fake AuditLogClient."""
    import time

    ctx = AuditScriptContext(
        short_name="hipaa-isolation",
        sa_email="asb-audit-sensitive-iso@agency-brain-demo.iam.gserviceaccount.com",
        project_id="agency-brain-demo",
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    return DriftReporter(ctx)


def _stdout_payloads(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# run_check covers every CHECK and routes by table marker
# ---------------------------------------------------------------------------


def test_run_check_returns_empty_lists_when_no_drift():
    bq = _FakeBQClient(table_to_rows={})
    findings = hipaa_isolation_check.run_check(project_id="agency-brain-demo", bq_client=bq)

    assert set(findings.keys()) == {"accounts", "contacts", "contracts", "projects", "tasks"}
    assert all(rows == [] for rows in findings.values())
    # Every check actually executed (one query per CHECK).
    assert len(bq.queries) == 5


def test_run_check_surfaces_hipaa_account_match():
    bq = _FakeBQClient(
        table_to_rows={
            "airtable_replica.accounts": [
                _Row({"_airtable_record_id": "recHIPAA1", "company_name": "Hospice LLC"})
            ],
        }
    )
    findings = hipaa_isolation_check.run_check(project_id="agency-brain-demo", bq_client=bq)

    assert findings["accounts"] == [
        {"_airtable_record_id": "recHIPAA1", "company_name": "Hospice LLC"}
    ]
    assert findings["contacts"] == []
    assert findings["contracts"] == []
    assert findings["projects"] == []
    assert findings["tasks"] == []


# ---------------------------------------------------------------------------
# main() wiring — happy path, breach, error
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BRAIN_PROJECT_ID", "agency-brain-demo")
    monkeypatch.setenv(
        "AUDIT_SA_EMAIL",
        "asb-audit-sensitive-iso@agency-brain-demo.iam.gserviceaccount.com",
    )


def _patch_main_dependencies(monkeypatch, *, bq_client: Any, audit_log: AuditLogClient):
    """Patch the bigquery and audit-log entry points used by main()."""
    import google.cloud.bigquery as _bq

    class _ClientFactory:
        def __init__(self, *_, **__):
            pass

    monkeypatch.setattr(_bq, "Client", lambda *a, **k: bq_client, raising=True)
    monkeypatch.setattr(
        hipaa_isolation_check,
        "AuditLogClient",
        lambda **kw: audit_log,
        raising=True,
    )


def test_main_happy_path_emits_audit_ok(env, monkeypatch, capsys):
    bq = _FakeBQClient(table_to_rows={})
    audit_log = _RecordingAuditLog()
    _patch_main_dependencies(monkeypatch, bq_client=bq, audit_log=audit_log)

    rc = hipaa_isolation_check.main()

    assert rc == 0
    assert len(audit_log.emitted) == 1
    event = audit_log.emitted[0]
    assert event.agent_id == "audit-hipaa-isolation"
    assert event.success is True
    assert event.hipaa_guard_status is HipaaGuardStatus.PASSED
    payload = json.loads(event.output)
    assert payload["event_id"] == AuditEventId.SECURITY_AUDIT_OK.value
    assert payload["counts"] == {
        "accounts": 0,
        "contacts": 0,
        "contracts": 0,
        "projects": 0,
        "tasks": 0,
    }

    # Stdout: exactly one JSON line, INFO severity, no HIPAA_GUARD_TRIPPED.
    payloads = _stdout_payloads(capsys.readouterr().out)
    assert len(payloads) == 1
    assert payloads[0]["event"] == AuditEventId.SECURITY_AUDIT_OK.value
    assert payloads[0]["severity"] == "INFO"


def test_main_breach_emits_hipaa_guard_tripped(env, monkeypatch, capsys):
    bq = _FakeBQClient(
        table_to_rows={
            "airtable_replica.accounts": [
                _Row({"_airtable_record_id": "recHIPAA1", "company_name": "Hospice LLC"})
            ]
        }
    )
    audit_log = _RecordingAuditLog()
    _patch_main_dependencies(monkeypatch, bq_client=bq, audit_log=audit_log)

    rc = hipaa_isolation_check.main()

    assert rc == 2
    assert len(audit_log.emitted) == 1
    event = audit_log.emitted[0]
    assert event.success is True  # script ran; finding was a breach
    assert event.hipaa_guard_status is HipaaGuardStatus.TRIPPED
    payload = json.loads(event.output)
    assert payload["event_id"] == AuditEventId.HIPAA_GUARD_TRIPPED.value
    assert payload["counts"]["accounts"] == 1

    # Stdout payload must satisfy the alerts_hipaa.tf log-based metric filter.
    payloads = _stdout_payloads(capsys.readouterr().out)
    assert len(payloads) == 1
    assert payloads[0]["event"] == "HIPAA_GUARD_TRIPPED"
    assert payloads[0]["severity"] == "ERROR"


def test_main_error_emits_audit_error_and_returns_nonzero(env, monkeypatch, capsys):
    bq = _RaisingBQClient(RuntimeError("simulated GCP failure"))
    audit_log = _RecordingAuditLog()
    _patch_main_dependencies(monkeypatch, bq_client=bq, audit_log=audit_log)

    rc = hipaa_isolation_check.main()

    assert rc == 1
    assert len(audit_log.emitted) == 1
    event = audit_log.emitted[0]
    assert event.success is False
    assert event.error and event.error.startswith("RuntimeError")
    payload = json.loads(event.output)
    assert payload["event_id"] == AuditEventId.SECURITY_AUDIT_ERROR.value

    payloads = _stdout_payloads(capsys.readouterr().out)
    assert len(payloads) == 1
    assert payloads[0]["event"] == AuditEventId.SECURITY_AUDIT_ERROR.value


# ---------------------------------------------------------------------------
# Reporter contract: emits at most one event per run
# ---------------------------------------------------------------------------


def test_reporter_rejects_double_emit():
    audit_log = _RecordingAuditLog()
    reporter = _build_reporter(audit_log)

    reporter.ok({"counts": {}})

    with pytest.raises(RuntimeError, match="already emitted"):
        reporter.ok({"counts": {}})


def test_reporter_stdout_payload_matches_log_metric_filter():
    """The alerts_hipaa.tf log-based metric matches `jsonPayload.event:"HIPAA_GUARD_TRIPPED"`.

    A breach-emitted line must serialize with that exact key/value so the
    metric counts it without any TF change in PR #2.
    """
    audit_log = _RecordingAuditLog()
    reporter = _build_reporter(audit_log)

    buf = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf):
        reporter.hipaa_breach({"counts": {"accounts": 1}})

    payload = json.loads(buf.getvalue().strip())
    assert payload["event"] == "HIPAA_GUARD_TRIPPED"
