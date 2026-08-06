# ADR 0021 — Minimal CRM read-sync into airtable_replica (superseded, never implemented)

**Status:** Superseded by ADR 0020
**Date:** 2026-04-29
**Workstream:** WS-B (Data Pipeline) → enables WS-G1 PR 4g (Triage Agent project resolver)
**Related:** ADR 0011 (two-base architecture — anticipated this PAT), ADR 0010 (Cloud Run Job host model — unchanged), ADR 0020 (collapses the two bases into one — retires this CRM-specific sync)

## Context

The Triage Agent (WS-G1) needs to resolve a Gmail signal's sender to an Airtable Project so it can draft a Task with the right `Project` link. The resolver chain is:

```
Gmail.sender → CRM Contact → CRM Account → Operations Client → Operations Project
```

The first two steps require CRM data the Brain currently does not see. ADR 0011 anticipated this: *"Two PATs eventually — one for the Operations base (the sync uses this), one for the CRM base ... v1 ships with only the Operations PAT."* This ADR is the realization of that step, scoped tightly.

The CRM base (`appj1Bo8Uw6Oa6MwE`) has 4 tables: Accounts (2 rows), Contacts (2 rows, 1 fully populated), Contracts (3 rows), Leads/Opportunities (0 rows). The CRM is the canonical store for company/contact metadata; we do not propose to mirror it richly, only to sync the slice the Brain needs to join against Operations.

## Decision summary

1. **Sync CRM Accounts, Contacts, Contracts** into `airtable_replica.crm_*` BigQuery tables. Skip Leads/Opportunities for v1 — empty, and not on any agent's near-term context path.
2. **Generalize the existing `asb-airtable-sync` Cloud Run Job** to iterate over a list of `(base_id, pat_secret_id, schema_path, tables)` configs in a single invocation. One scheduler tick → both bases sync sequentially. No second job, no second scheduler.
3. **CRM data is HIPAA-untyped at the source** — the CRM has no `HIPAA` field. The HIPAA cascade lands at the **resolver-view layer**: `sender_to_project_v` joins through `Operations.Clients` and inherits its HIPAA filter (rows for HIPAA-flagged clients are already absent from `airtable_replica.clients` per PRD §4.1 layer 2). The view applies `WHERE COALESCE(c.hipaa_excluded, FALSE) = FALSE` defensively.
4. **CRM table BQ ids prefixed `crm_`** to keep namespace clear and avoid future collisions (e.g. an Operations table accidentally also named "Contracts"). Implemented via a `_bq_table_id` per-table override in `crm_schema.json`.
5. **Two PATs, two secrets.** Existing `airtable-pat-prod` (Operations, read-only) is unchanged. New `airtable-crm-pat-prod` (CRM, read-only) is granted `secretmanager.secretAccessor` to the existing `asb-sync-airtable-sa`. Same SA reads both bases — least-privilege boundary stays narrow because both bases are read-only and the SA already has BQ write on the single replica dataset.
6. **Drift detection unchanged.** New CRM fields land in `asb-schema-drift-alerts` like Operations fields do. `crm_schema.json` declares the full CRM field set (not just what we technically read) so drift fires only on truly-new fields, not on every sync.

## Why generalize the existing job (not a second job)

| Option | Cost | Complexity | Verdict |
|---|---|---|---|
| Second Cloud Run Job + scheduler | More TF, two schedules to keep aligned, two log streams to correlate | Medium | Rejected |
| **Generalize existing job** | One config loop, two HTTP-target schedules collapse to one | Low | **Accepted** |

The two-base sync runs sequentially in the same process. Total runtime is bounded by the API call count (low-tens of records per cycle) plus BQ load jobs — well under the existing 900s timeout. If runtime grows past ~5 min as data volume increases, splitting into parallel jobs is a one-day refactor.

## Why a `_bq_table_id` override (not table renames)

The Airtable API call uses the schema-key as the literal table name (`/v0/{base}/Accounts`). Renaming the schema key to "CRM Accounts" would require a second decoupling field for the API call. A `_bq_table_id` override on the schema key keeps the API name stable while letting the BQ id carry the namespace prefix. Cleanest decoupling, smallest code change.

## What this ADR does not do

- **Does not wire the resolver into the Triage Agent.** That's PR 4g (deferred), gated on this PR's data foundation.
- **Does not sync Leads/Opportunities.** Empty today; add in a follow-up when the CRM starts populating it and an agent needs the data.
- **Does not introduce per-CRM-table HIPAA filters at the source.** The CRM has no HIPAA field; the cascade is enforced at the join in `sender_to_project_v`. This is correct for current data shape (no HIPAA clients) but should be revisited if a HIPAA marker is ever added to the CRM directly.
- **Does not auto-create CRM Contacts when the Triage resolver misses.** Drafts-only boundary (PRD §4.7) holds — humans backfill the CRM, the resolver picks them up on the next sync.

## Consequences

- A second Secret Manager secret (`airtable-crm-pat-prod`) and a second IAM binding on the existing sync SA. The existing PR-gate `least_privilege_check.py` validates the SA still holds only the custom `tbSyncAirtable` role plus narrow resource-scoped bindings.
- Three new BQ tables in `airtable_replica`: `crm_accounts`, `crm_contacts`, `crm_contracts`. Same `WRITE_TRUNCATE` semantics as Operations tables — running the sync twice produces the same end state.
- One new materialized view: `airtable_replica.sender_to_project_v`. Daily-staleness pattern matches `client_owners_v`.
- The expected day-1 resolver hit rate is **near zero** because the CRM has 1 fully-populated Contact today. The infrastructure is correct; data quality catches up as engagements happen and the operator backfills Contacts. The Triage Agent's no-match policy (decided in PR 4g) is the load-bearing one for v1, not the resolver itself.

## Revisit if

- CRM Contacts grow to where the resolver becomes the dominant code path — at which point we should probably move to event-driven sync (Airtable webhooks) rather than 15-min polling.
- A HIPAA flag is added to the CRM directly (e.g. compliance requirements force per-Account HIPAA marking) — at which point CRM tables get their own `filterByFormula` HIPAA cascade.
- The Brain starts WRITING to the CRM (e.g. auto-creating Contacts from approved drafts) — at which point ADR 0011's drafts-only stance needs a follow-up ADR.
