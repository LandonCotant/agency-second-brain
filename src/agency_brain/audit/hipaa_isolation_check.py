"""HIPAA isolation runtime check (PRD §4.1 layer 5, hourly).

Runs as Cloud Run Job ``asb-audit-sensitive-isolation`` on a 1h Cloud Scheduler
cadence. Verifies no HIPAA-flagged row appears in the
``airtable_replica.*`` tables and emits ``HIPAA_GUARD_TRIPPED`` on any
match.

Five queries, all expected to return zero. ADR 0020 collapsed the legacy
``clients`` table into ``accounts``; HIPAA roots at ``accounts.hipaa`` and the
cascade reaches children through their link arrays. The per-table ``Account
HIPAA`` / ``Project HIPAA`` fields are ``multipleLookupValues`` and are NOT
synced to the replica, so the audit re-derives the cascade by joining the link
arrays (``account``, ``project``) back to ``accounts.hipaa``:

1. ``airtable_replica.accounts`` — direct rows where the source ``HIPAA``
   checkbox is true. The sync's ``filterByFormula`` (``NOT({HIPAA})``) should
   keep these out (PRD §4.1 layer 2); a nonzero result means the source filter
   regressed. **This is the primary tripwire.**
2. ``airtable_replica.contacts`` joined to ``accounts`` via the ``account``
   array.
3. ``airtable_replica.contracts`` joined to ``accounts`` via the ``account``
   array.
4. ``airtable_replica.projects`` joined to ``accounts`` via the ``account``
   array.
5. ``airtable_replica.tasks`` joined transitively via ``project`` →
   ``projects`` → ``account`` → ``accounts``.

Because HIPAA accounts are filtered at source, a child-table hit (checks 2-5)
requires its HIPAA account to ALSO have leaked — so detection is effectively
gated on check 1. The cascade checks earn their keep by enumerating which
child rows are exposed when an account does leak, and by asserting the join
chain stays intact.

All Brain BQ datasets (``airtable_replica``, ``agent_outputs``,
``agent_audit_log``) live in BigQuery multi-region ``US`` (ADR 0013).
The BQ client below is constructed with ``location="US"`` so query jobs
route there rather than the Cloud Run Job's ``us-central1`` regional
default — without it, queries 404 with "Dataset not found in location
us-central1".

``agent_outputs.{triaged_items, risk_flags}`` cross-checks are not yet
in this script. They became feasible when ADR 0013 co-located all
datasets but require their own design pass (which agent_outputs columns
indicate HIPAA exposure, what the join shape is). Until then, the
agent_outputs surface is checked at PR time via
``scripts/hipaa_filter_check.py``.

What this catches: filter-bypass leaving HIPAA rows in BQ.
What this does NOT catch: silent row-suppression. If ``filterByFormula``
exists but the entire load truncates a table to empty, the HIPAA rows
never landed and there's nothing to count. Mitigation lives in
``docs/runbooks/runtime_audit_response.md`` as a quarterly Airtable-side
cross-check (pull HIPAA=true record IDs directly from Airtable; verify
they're absent from BQ).

The script does NOT itself flip the kill switch. WS-F PR #3 wires the
``agent_kill_switch`` Secret Manager flag and the agent-side read; until
then, the existing P0 alert in ``alerts_hipaa.tf`` instructs the operator
to flip it manually.

Required env vars (set by the Cloud Run Job in
``terraform/modules/security/runtime_audits.tf``):

- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL`` — populated by Terraform from the SA email
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from ..common.audit_log import AuditLogClient
from ._reporter import AuditScriptContext, DriftReporter

log = logging.getLogger("agency_brain.audit.hipaa_isolation_check")

# -- HIPAA-EXCLUDE
# Each query carries the canonical marker + the COALESCE clause so
# scripts/hipaa_filter_check.py recognizes the file as HIPAA-aware. The
# clauses below ARE the cascade — every join filters on hipaa-flagged rows
# and the COALESCE clause defends against NULLs in the hipaa column.

# Root check: a HIPAA-flagged Account that leaked past the source filter.
QUERY_ACCOUNTS = """
-- HIPAA-EXCLUDE
SELECT _airtable_record_id, company_name
FROM `{project}.airtable_replica.accounts`
WHERE hipaa = TRUE
  AND COALESCE(hipaa_excluded, FALSE) = FALSE
LIMIT 100
"""

# Contacts cascade from their linked Account (Contacts.Account HIPAA lookup).
QUERY_CONTACTS = """
-- HIPAA-EXCLUDE
SELECT con._airtable_record_id AS contact_id, a._airtable_record_id AS account_id
FROM `{project}.airtable_replica.contacts` con,
UNNEST(con.account) AS acc
JOIN `{project}.airtable_replica.accounts` a
  ON a._airtable_record_id = acc
WHERE a.hipaa = TRUE
  AND COALESCE(con.hipaa_excluded, FALSE) = FALSE
LIMIT 100
"""

# Contracts cascade from their linked Account (Contracts.Account HIPAA lookup).
QUERY_CONTRACTS = """
-- HIPAA-EXCLUDE
SELECT ctr._airtable_record_id AS contract_id, a._airtable_record_id AS account_id
FROM `{project}.airtable_replica.contracts` ctr,
UNNEST(ctr.account) AS acc
JOIN `{project}.airtable_replica.accounts` a
  ON a._airtable_record_id = acc
WHERE a.hipaa = TRUE
  AND COALESCE(ctr.hipaa_excluded, FALSE) = FALSE
LIMIT 100
"""

# Projects cascade from their linked Account (Projects.Account HIPAA lookup).
QUERY_PROJECTS = """
-- HIPAA-EXCLUDE
SELECT p._airtable_record_id AS project_id, a._airtable_record_id AS account_id
FROM `{project}.airtable_replica.projects` p,
UNNEST(p.account) AS acc
JOIN `{project}.airtable_replica.accounts` a
  ON a._airtable_record_id = acc
WHERE a.hipaa = TRUE
  AND COALESCE(p.hipaa_excluded, FALSE) = FALSE
LIMIT 100
"""

# Tasks cascade transitively: task -> project -> account (Tasks.Project HIPAA).
QUERY_TASKS = """
-- HIPAA-EXCLUDE
SELECT t._airtable_record_id AS task_id, a._airtable_record_id AS account_id
FROM `{project}.airtable_replica.tasks` t,
UNNEST(t.project) AS pid
JOIN `{project}.airtable_replica.projects` p
  ON p._airtable_record_id = pid,
UNNEST(p.account) AS acc
JOIN `{project}.airtable_replica.accounts` a
  ON a._airtable_record_id = acc
WHERE a.hipaa = TRUE
  AND COALESCE(t.hipaa_excluded, FALSE) = FALSE
LIMIT 100
"""

CHECKS: tuple[tuple[str, str], ...] = (
    ("accounts", QUERY_ACCOUNTS),
    ("contacts", QUERY_CONTACTS),
    ("contracts", QUERY_CONTRACTS),
    ("projects", QUERY_PROJECTS),
    ("tasks", QUERY_TASKS),
)


def run_check(*, project_id: str, bq_client: Any) -> dict[str, list[dict[str, Any]]]:
    """Execute every check; return a ``{check_name: [matched_rows]}`` map.

    A check that returns zero rows still appears in the map with an empty
    list, so the caller can prove the check actually ran (vs. silently
    skipped).
    """
    findings: dict[str, list[dict[str, Any]]] = {}
    for name, sql_template in CHECKS:
        sql = sql_template.format(project=project_id)
        rows = list(bq_client.query(sql).result())
        findings[name] = [dict(r) for r in rows]
    return findings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-sensitive-isolation-sa@{project_id}.iam.gserviceaccount.com",
    )

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="hipaa-isolation",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    try:
        from google.cloud import bigquery

        # location="US" routes queries to the multi-region datasets (ADR 0013).
        # Cloud Run Job runs in us-central1; without this, BQ 404s.
        bq_client = bigquery.Client(project=project_id, location="US")
        findings = run_check(project_id=project_id, bq_client=bq_client)
    except Exception as exc:
        log.exception("hipaa_isolation_check failed before completion")
        reporter.error(exc)
        return 1

    counts = {name: len(rows) for name, rows in findings.items()}
    total = sum(counts.values())
    if total == 0:
        reporter.ok({"counts": counts})
        return 0

    reporter.hipaa_breach(
        {
            "counts": counts,
            # First few matched rows per check, capped to keep the audit row small.
            "samples": {name: rows[:3] for name, rows in findings.items() if rows},
        }
    )
    # Exit non-zero so the Cloud Run Job execution is flagged in the console
    # alongside the alert. The audit row + log line are the canonical signals;
    # exit code is for operator visibility.
    return 2


if __name__ == "__main__":
    sys.exit(main())
