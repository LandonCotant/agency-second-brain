# sync — Data ingestion flows (WS-B)

Owns the sync layer between source systems and BigQuery / Pub/Sub.

## Status

| PR  | Status | Lands |
|-----|--------|-------|
| PR-1 | shipped | `airtable/schema.json` + `validation_rules.md`; `airtable_replica` BQ dataset; `asb-triage-input` (+ DLQ), `asb-schema-drift-alerts` topics; Knowledge Catalog aspect types (spec §10.2) |
| PR-2 | shipped | `airtable_to_bq.py` + `asb-sync-airtable-sa` + replica table DDL (derived from `schema.json`) + HIPAA isolation security test + Cloud Run Job + Cloud Scheduler |
| PR-3 | pending | `vantage_federation.py` + ADR for cross-project topology (PRD Appendix B) |
| PR-4 | pending | `workspace_to_pubsub.py` + DWD scopes + HIPAA domain filter |
| PR-5 | pending | Aspect application to `airtable_replica.*` tables + Drive/Gmail entries |


| Module | Source | Destination | Trigger | Host |
|---|---|---|---|---|
| `airtable_to_bq.py` | Airtable | `airtable_replica.*` | Cloud Scheduler `*/15 * * * *` | Cloud Run Job (ADR 0010) |
| `vantage_federation.py` | Vantage authorized view | `vantage_kpi_snapshots` | Cloud Scheduler (daily) | Cloud Run Job (planned) |
| `workspace_to_pubsub.py` | Gmail / Calendar / Chat / Drive | `asb-triage-input` topic | Application Integration trigger | Application Integration |

Per [PRD.md](../../../../PRD.md) §6.2:
- Pull strategy: full pull per cycle, always (table sizes don't justify incremental — see ADR 0010). The half-wired `IS_AFTER(checkpoint)` filter was removed 2026-06-10: it was inert (no-op checkpoint MERGE) and would have truncated the replica to the delta under WRITE_TRUNCATE had it ever activated.
- Idempotency: `WRITE_TRUNCATE` load job per table per run for PR-2; `MERGE` is the future evolution if row counts grow.
- HIPAA filter applied at the source query via Airtable Lookup fields (`Projects.Client HIPAA`, `Tasks.Project HIPAA`) — not post-hoc. See `airtable/validation_rules.md` and `tests/security/test_hipaa_isolation.py`.
- Schema drift surfaced via `asb-schema-drift-alerts` Pub/Sub topic; never auto-added.

## Service accounts

- `asb-sync-airtable-sa` (PR-2 — shipped) — Custom role `tbSyncAirtable`
  (`bigquery.jobs.create`, `bigquery.datasets.get` at project scope).
  Resource-scoped bindings: `roles/bigquery.dataEditor` on
  `airtable_replica` only, `roles/secretmanager.secretAccessor` on the
  Airtable PAT secret only, `roles/pubsub.publisher` on
  `asb-schema-drift-alerts` only.
- `asb-airtable-sync-invoker` (PR-2 — shipped) — Cloud Scheduler's OIDC
  identity. Holds `roles/run.invoker` on the `asb-airtable-sync` job only.
- `asb-sync-vantage-sa` (PR-3 — pending) — BigQuery query on the Vantage
  authorized view; BigQuery write to `vantage_kpi_snapshots` only.

All SAs are provisioned in this workstream's Terraform module.

## Operations

- Container image is built by Cloud Build on every PR (verification only).
  Push + Cloud Run Job rollout is manual today: `gcloud run jobs update
  asb-airtable-sync --image=...`. The Cloud Run Job's
  `lifecycle.ignore_changes` on the image attribute lets new tags roll
  forward without Terraform fighting them.
- PAT rotation: `docs/runbooks/airtable_pat_rotation.md`.
- Live HIPAA isolation verification: `docs/runbooks/hipaa_isolation_verification.md`.
