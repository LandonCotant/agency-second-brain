# ADR 0013 — Unify agent dataset locations to US multi-region

**Status:** Accepted
**Date:** 2026-04-28
**Workstream:** WS-C (Agent Runtime)

## Context

Two BigQuery datasets the agents write to (`agent_audit_log`,
`agent_outputs`) were created at `location = var.region`, which resolves
to `us-central1`. The third dataset they read from
(`airtable_replica`) was created at `location = "US"` (multi-region) by
the WS-B Terraform module.

BigQuery does not allow JOINs across datasets in different locations,
even when one is a regional location nested inside the multi-region of
the other. Any query that references both an agent dataset and the
replica fails with `404 Not found: Dataset … was not found in location
us-central1`.

PR #13 (commit `d12eb4e`) worked around this by setting `location="US"`
on the BigQuery client in `hipaa_isolation_check.py` and disabling the
cross-dataset queries in `QUERY_TRIAGED_ITEMS` / `QUERY_RISK_FLAGS`.
That kept the audit script alive but left the underlying limitation in
place — every future agent that needs to correlate replica state with
agent outputs would hit the same wall.

## Decision

Recreate `agent_audit_log` and `agent_outputs` at `location = "US"` so
all three datasets share a single multi-region. Implemented by changing
the literal in `terraform/modules/agent_runtime/main.tf` from
`var.region` to `"US"` for both dataset resources. `var.region` continues
to control Cloud Run / Scheduler resource placement, which stays in
`us-central1`.

BigQuery datasets cannot be relocated in place. Migration:

1. Snapshot `agent_audit_log.events` (9 rows) to GCS via `bq extract`.
2. Clear `deletionProtection` on the 5 contained tables via the BQ REST
   API (the `bq` CLI doesn't expose this flag).
3. `bq rm -r -f -d` both datasets.
4. `terraform apply -target=` the 2 datasets + 5 tables to recreate
   them at `US`. (Targeted because the env has 27 unrelated resources
   defined in TF that have never been applied — out of scope for this
   migration.)
5. `bq load` the snapshot back into the new `agent_audit_log.events`.
6. Verify cross-dataset JOIN to `airtable_replica` succeeds.

`agent_outputs` was empty so step 1/5 were skipped for it.

## Why US multi-region (not us-central1 for everything)

- `airtable_replica` was already at `US` and carries the larger of the
  two row counts. Moving it would have meant pausing the (yet-to-deploy)
  `asb-airtable-sync` job and recopying 8 tables.
- US multi-region is BigQuery's recommended default for analytics
  workloads — slightly higher durability via dual-region replication,
  same query pricing, no slot-pool difference at our volume.
- Compute (Cloud Run, Scheduler) doesn't need to match BQ location.
  Keeping those at `us-central1` keeps egress to BQ free (multi-region
  egress from a contained region is free).

## Why not a dual-write shim during the cutover

The original migration plan mentioned a "brief read-only window or an
idempotent dual-write shim" as the two options. The window won because:

- The four audit Cloud Run Jobs that write to `agent_audit_log` were
  defined in `terraform/modules/security/runtime_audits.tf` but had
  never been applied to GCP (`gcloud run jobs list` returned 0). So no
  writers were active during the cutover — the window was effectively
  permanent until those jobs deploy.
- A dual-write shim would have meant Python changes in `AuditLogClient`
  for a one-time migration. CLAUDE.md's "don't do enterprise ceremony
  for a 2-person tool" guidance argues against it.

## Consequences

- `AuditLogClient` (`src/agency_brain/common/audit_log.py`) needs no
  changes. It instantiates `bigquery.Client(project=…)` without a
  `location=` argument and lets `insert_rows_json` resolve location
  from the table reference. This works identically against `US` and
  `us-central1`.
- `hipaa_isolation_check.py:26-34` still carries the cross-region skip
  and an explicit `location="US"` on its BQ client. Both can be removed
  in a follow-up PR — kept separate so this migration and the code
  un-skip can be reverted independently.
- Future agent datasets should default to `location = "US"` rather than
  `var.region`. The `var.region` pattern is appropriate for compute
  resources (Cloud Run, Scheduler, Artifact Registry) but is the wrong
  default for BigQuery in a project that needs cross-dataset queries.

## References

- PRD §6.1 (agent_outputs schema)
- PRD §4.6 (audit log layer 2)
- ADR 0009 (mixed-canonicality `agent_outputs.*` schema design)
- PR #13 (`fix(audit): location=US for hipaa_isolation BQ client; drop
  cross-region queries`) — the workaround this ADR supersedes the need
  for.
