# ADR 0010 — Cloud Run Job for the Airtable → BigQuery sync

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-B (Data Pipeline)

## Context

Spec §9.1 sketches the sync flows as "Application Integration" (the GCP
managed-iPaaS product). PRD §6.2 leaves the execution model unspecified and
focuses on the contractual properties: HIPAA filter at source, MERGE
idempotency, schema drift surfaced not auto-applied.

`airtable_to_bq` is a Python script that calls Airtable's REST API and
writes rows to BigQuery. Three viable hosts:

1. **Application Integration flow** — matches the spec literally; the Python
   logic still has to live somewhere callable, and the flow definition lives
   outside the repo (XML in the GCP console). Not code-reviewable in the
   normal sense.
2. **Cloud Functions (2nd gen)** — serverless container, 60-min timeout, fits
   small jobs. Slightly thinner than Cloud Run on knobs and cold-start
   tradeoffs.
3. **Cloud Run Job** — containerized Python, up to 24h timeout, OIDC-invoked
   from Cloud Scheduler, native env-var + Secret Manager wiring. The
   standard GCP host for batch / cron workloads in 2025+.

## Decision

Run `airtable_to_bq` as a **Cloud Run Job** triggered by Cloud Scheduler
every 15 minutes.

- Image: `${region}-docker.pkg.dev/${project}/asb-sync/airtable-to-bq`
  built from the repo `Dockerfile`.
- SA: `asb-sync-airtable-sa` (custom role `tbSyncAirtable`).
- Trigger: `google_cloud_scheduler_job.tb_airtable_sync_15m` posts to the
  job's `:run` endpoint with OIDC auth as `asb-airtable-sync-invoker`.
- Replica writes use `WRITE_TRUNCATE` load jobs — full-snapshot replacement
  per table per run. The acceptance contract ("re-run is a no-op", "HIPAA
  flip removes within one cycle") falls out of full snapshots for free, and
  replica tables are small (low hundreds of rows) so cost difference vs. an
  incremental MERGE is negligible.

## Why not Application Integration

- Flow definitions don't live in the repo; PR review can't see the logic.
- Python code still needs a host; AI-only path through Application
  Integration script tasks is more constrained than Cloud Run.
- The 15-min cadence and the cron-like nature of the job are the natural
  use case for a Cloud Run Job, not the long-running event-driven model
  Application Integration is designed for.

## Why not Cloud Functions

- Cloud Run Jobs and Cloud Functions converge on near-identical mechanics
  for this workload (containerized Python, OIDC trigger). Cloud Run Jobs
  give a longer max timeout (24h vs 60min) and `gcloud run jobs execute`
  is the standard ops affordance for cron-style work — the operator can run the
  job ad-hoc to verify a sync without waiting for the scheduler tick.

## Consequences

- The repo owns a `Dockerfile` and an Artifact Registry repo (`asb-sync`).
  Both add some surface area but are standard.
- The first `terraform apply` requires a placeholder image (`bootstrap`
  tag) so the Cloud Run Job resource can come up before Cloud Build has
  pushed a real one. The Cloud Run Job's `lifecycle.ignore_changes` on the
  image attribute lets the build pipeline roll new tags forward without
  Terraform fighting them.
- `WRITE_TRUNCATE` means no row history in the replica — that's intended,
  the replica is a current-state mirror, not an SCD2. If a downstream
  consumer needs Airtable history, they should query Airtable directly or
  build their own snapshot table (out of scope for v1).
- The `_sync_checkpoints.last_checkpoint` column is currently unused
  (always NULL) because PR-2 does full pulls. The plumbing for
  `IS_AFTER({last_modified}, ...)` is in `hipaa_filters.py` and will
  light up once we add `lastModifiedTime` fields to every Airtable table
  and switch from `WRITE_TRUNCATE` to `MERGE` — deferred to a future PR
  if/when row counts justify it.

## References

- PRD §4.1 layer 2 (HIPAA filter at source query)
- PRD §4.2, §4.8 (least-privilege custom IAM role)
- PRD §6.2 (sync flow contract)
- ADR 0006 (audit log streaming) — establishes the precedent of preferring
  the simpler, code-reviewable mechanism over heavier managed services
  when both meet the contractual properties.
