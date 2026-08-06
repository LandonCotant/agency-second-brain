# ADR 0055 — HIPAA isolation audit dormant until HIPAA ingestion ships

**Status:** Accepted — 2026-05-18. Complements ADR 0012 (runtime audit architecture); does not supersede it. Driven by alert spam caused by the 2026-05-13 calendar-silent alert policy interacting with the deferred-HIPAA posture.

## Context

`asb-audit-sensitive-isolation` is PRD §4.1 layer-5 — a hourly BigQuery check that no HIPAA-flagged Airtable rows leak into the replica tables that agents read. The job is wired up per ADR 0012: Cloud Run Job + per-script SA + hourly Cloud Scheduler trigger (`0 * * * *` UTC), emitting `SECURITY_AUDIT_OK` on a clean tick and `HIPAA_GUARD_TRIPPED` (which routes to the P0 alert in `alerts_hipaa.tf`) on a breach.

Since at least 2026-05-09, **every execution has failed** with:

```
google.api_core.exceptions.NotFound: 404 Not found: Table
agency-brain-demo:airtable_replica.clients was not found in location US
```

Two reasons the table doesn't exist:

1. **HIPAA ingestion is deferred** for this build (CLAUDE.md "Safety rails" + project memory `project_hipaa_deferred`). No HIPAA-flagged records are being synced; no consuming agent looks at HIPAA fields yet.
2. **The Operations base uses `accounts` not `clients`** (ADR 0020, 2026-04-30). The audit code was written against a pre-collapse schema that no longer exists. `airtable_replica.accounts` is what carries the HIPAA cascade root in current prod.

The 2026-05-13 `Cloud Run job execution error` alert policy (added after the calendar-silent incident burned four days of ingestion) fires on ERROR count ≥ 1 over 5 min across all Cloud Run Jobs — no per-job exclusion. Combined with the hourly cron, the user was receiving ~24 alert emails per day plus matching Chat pings from this one failing job.

## Decision

Pause the `asb-audit-sensitive-isolation-cron` Cloud Scheduler via Terraform until HIPAA ingestion is actually wired up. The Cloud Run Job, its SA, the custom IAM role, the audit-log destination, and the image stay deployed — only the cron trigger is suppressed. Flipping the audit back on is a one-line change + targeted apply.

Implementation: `terraform/modules/security/runtime_audits.tf` now carries a `paused` field on each entry in `local.audit_jobs` (default `false`), wired into `google_cloud_scheduler_job.audit` via `paused = each.value.paused`. `hipaa_isolation.paused = true`; the other four audits stay live.

## Alternatives rejected

**Fix the audit code against the real schema (`accounts` instead of `clients`).** Tempting — the queries are short and the rename is mechanical — but contradicts `project_hipaa_deferred`. The whole point of layer-5 is to catch HIPAA leakage; if no HIPAA rows are being ingested, the check is asserting an invariant over an empty set and provides zero defensive value. We'd be writing code that runs hourly in prod for no current load-bearing reason, and we'd still have to rewrite it when HIPAA ingestion actually ships (we don't yet know which table will carry the flag — could be `accounts.hipaa`, could be a new dedicated table). Better to leave the rewrite for when there's a real consumer to verify against.

**Graceful skip in the audit code when tables are missing.** Add a `try/except NotFound: emit SECURITY_AUDIT_SKIPPED, exit 0`. Stops the spam without changing the scheduler. Rejected because it adds dead-code we'd have to remember to rip out when HIPAA ingestion ships, and "skip silently when missing" is the exact failure-mode that caused the 2026-05-13 calendar-silent incident — we don't want to teach the codebase that pattern for an audit, of all things.

**Suppress this job in the alert policy.** Add a per-job exclusion to `asb-cloud-run-job-error` so this one job's ERRORs don't page. Rejected because the audit would keep failing every hour, polluting `agent_audit_log.events` and BQ query history with garbage rows, and the suppression filter would become a precedent ("add another exclusion when the next job is noisy") that erodes the alert's signal value. Pausing the source is cleaner.

## Re-enable checklist

When HIPAA ingestion is actually being implemented:

- [ ] Confirm which Airtable table(s) carry HIPAA-flagged records (`accounts.hipaa`? a new dedicated table?) and the exact column shape (`hipaa` BOOL? `hipaa_excluded` BOOL?).
- [ ] Verify the sync materializes those columns in `airtable_replica` — `bq query --location=US 'SELECT hipaa FROM \`agency-brain-demo.airtable_replica.<table>\` LIMIT 5'`.
- [ ] Rewrite `src/agency_brain/audit/hipaa_isolation_check.py` queries against the real table + column names.
- [ ] Set `paused = false` for `hipaa_isolation` in `terraform/modules/security/runtime_audits.tf`.
- [ ] `terraform apply -target='module.security.google_cloud_scheduler_job.audit["hipaa_isolation"]'`.
- [ ] Smoke-fire one execution against real data: `gcloud run jobs execute asb-audit-sensitive-isolation --region=us-central1 --project=agency-brain-demo --wait`. Expect `SECURITY_AUDIT_OK` and zero alert emails.
- [ ] Update the `asb-audit-sensitive-iso` row in `docs/PRODUCTION_STATE.md` (drop the `PAUSED (ADR 0055)` marker; bump `Last verified:`).
- [ ] Optionally write a follow-up ADR closing 0055 if the rewrite materially changes the audit's surface.

## Related

- ADR 0012 — runtime audit architecture (this is the same architecture, just one of five jobs dormant)
- ADR 0020 — Airtable collapse to single Operations base (where the `clients` → `accounts` rename originated)
- Project memory `project_hipaa_deferred` — the standing instruction not to proactively fix failing HIPAA audits
- CLAUDE.md "Safety rails" — HIPAA isolation is the load-bearing guarantee once ingestion ships
