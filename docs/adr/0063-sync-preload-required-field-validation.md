# ADR 0063 — Pre-load required-field validation in the Airtable→BQ sync

**Status:** Accepted — 2026-06-05

> **Implementation status (2026-06-05):** Code + tests landed on branch
> `feat/sync-preload-required-validation` (`find_invalid_rows` in
> `airtable_to_bq.py`). **Pending cutover (operator):** rebuild/roll out the
> `asb-airtable-sync` image (`cloudbuild.airtable-sync.yaml`, `_TAG=adr-0063-v1`)
> → `gcloud run jobs update` to the new image → prod smoke. Rollback = redeploy
> the prior image tag (`fix-replica-cols-v1`).

**Workstream:** observability / data-pipeline robustness
**Related:** ADR 0010 (WRITE_TRUNCATE full-snapshot sync), ADR 0019 (Cloud Run Job + Scheduler bridge), the `asb-cloud-run-job-error` alert (observability/`alerts_runtime.tf`, added after the 2026-05-13 calendar silent-failure incident)

## Context

`asb-airtable-sync` replaces each replica table's contents with a `WRITE_TRUNCATE`
load job (ADR 0010). The load schema marks a column `REQUIRED` when the
`schema.json` field is `required: true`. BigQuery rejects a `WRITE_TRUNCATE`
load if **any** row has `NULL` in a `REQUIRED` column (`Only optional fields can
be set to NULL`), and the rejection fails the **entire table load** — not just
the offending row.

This failure mode has now caused **six** incidents. The recurring shape: a
half-entered Airtable record (a required field left blank) silently halts the
whole table's sync. Most were "fixed" by relaxing the column to `NULLABLE`
(`Tasks.Owner`, `Tasks.Source`, `Captures.Note Text`, `Tasks.Action Type`,
`Goals.Owner`).

On **2026-06-05** a new client "Houston Gilbert" was entered with empty
`Segment`/`Status`/`Account Manager` (Account) and empty `Owner`/`Service`
(Project). Both the Accounts and Projects replicas stopped updating mid-day
until the record was completed. Two defects compounded:

1. **Blast radius** — one incomplete row blackholed every row in the table.
2. **Opaque signal** — the alert surfaced a raw BQ JSON error naming only the
   *first* missing field (`Field: segment`), so diagnosis required manually
   querying Airtable to enumerate the rest.

## Decision

Add a **pre-load validation pass** in `sync_one_table`, between row translation
and the load:

1. `find_invalid_rows` partitions translated rows into **valid** (every
   `REQUIRED`, non-`BOOL`, non-`REPEATED` column populated) and **invalid**.
2. **Only valid rows are loaded.** The table keeps syncing its complete records;
   one half-entered row no longer blackholes the table.
3. **Each invalid row emits one `log.error`** naming the table, record id,
   primary-field label, and **all** missing fields by their Airtable names —
   e.g. `incomplete record skipped: table=Accounts record=recJpl… label='Houston
   Gilbert' missing_required=['Segment', 'Status', 'Account Manager']`.

Applies to **all** synced tables (`SYNC_TABLES_ORDER`), not just the two that
failed on 2026-06-05.

### Why validate-and-report, not filter-or-relax

- **Not relax-to-NULLABLE.** `Segment`, `Status`, `Account Manager`, `Owner`,
  `Service` are genuinely required, load-bearing fields (Account Manager drives
  routing, Segment drives Risk-Profile selection, Owner drives Task assignment).
  Relaxing them would let silently-incomplete client records flow into BQ and
  break downstream JOINs. The `Goals.Owner` relax was correct because strategic
  goals are *legitimately* ownerless; a client Account is not legitimately
  segment-less. NULLABLE is reserved for fields that are legitimately empty.
- **Not a sync-ready Airtable view filter.** Filtering incomplete records out of
  the pull trades today's *loud* failure for a *silent* one — the record would
  simply never appear in the Brain, and the gap would surface only when a brief
  or risk check came up empty (the exact 4-day-silent class the alert exists to
  prevent). The operator's explicit objection: "I'd probably just miss that they
  aren't ready." Validation keeps the signal loud *and* makes it specific.

### The green-execution / red-alert trade-off (deliberate)

When the only problem is dropped-for-incompleteness rows (no hard exception),
the job **exits 0** — the execution shows green while `asb-cloud-run-job-error`
goes red. This is accurate: the sync *ran fine and loaded every complete row*;
the *data* is incomplete. The log-based alert is the correct surface for a data
problem, and it re-fires every run until the record is completed in Airtable, so
it cannot be missed. Keeping exit 0 also avoids Cloud Run Job retry storms (a
non-zero exit would re-run the whole sync per the Job's retry policy). The
`SyncResult.invalid_rows` count records the drop in the run summary for
observability.

## Consequences

- **One incomplete record no longer blocks a table.** Complete rows keep
  flowing; the incomplete one is excluded until fixed, then reappears on the
  next sync.
- **The alert payload is now the fix-list** — record + all missing fields, no
  manual Airtable spelunking.
- **`schema.json` REQUIRED stays meaningful.** We stop reaching for NULLABLE as
  the default remedy; a field is NULLABLE only when it is legitimately optional.
- **An incomplete record is absent from BQ while incomplete.** Acceptable — an
  incomplete client record has no business in downstream JOINs anyway, and the
  alert nags until it is completed.
- **Deploy coupling.** Validation lives in the image; rollout requires an image
  rebuild + `gcloud run jobs update` (same discipline as any sync code change).
