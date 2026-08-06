# ADR 0025 — Separate `routed_events` table for WS-D dispatch tracking

**Status:** Accepted
**Date:** 2026-05-01
**Workstream:** WS-D (routing fan-out)

## Context

The original WS-D Chat fan-out design (ADR 0023) tracked dispatch state on the
classification row itself: `agent_outputs.triaged_items.routed_to STRING REPEATED`.
The fan-out worker would `UPDATE … SET routed_to = ARRAY_CONCAT(routed_to, [@channel])`
after a successful Chat send.

Smoke-testing on 2026-05-01 surfaced a hard BigQuery limitation:

> **DML statements (UPDATE / DELETE / MERGE) cannot operate on rows that are
> still in BigQuery's streaming buffer.** Rows inserted via the legacy streaming
> API (`tabledata.insertAll`, exposed in the Python SDK as `insert_rows_json`)
> sit in the streaming buffer for **30+ minutes** before being promoted to
> managed storage. Any UPDATE in that window fails with HTTP 400:
> `UPDATE or DELETE statement over table … would affect rows in the streaming
> buffer, which is not supported`.

The Triage Agent writes `triaged_items` rows via streaming insert
(`writers.py:146`). The fan-out polls every 5 minutes. When the first
`actionable=true / severity=critical` signal hit production, the fan-out:

1. Polled the row (now visible because streaming-buffer reads work).
2. Posted to the `Brain alerts` Chat space — **succeeded**.
3. Tried `UPDATE triaged_items SET routed_to = …` — **failed** (streaming buffer).
4. Treated the failure as transient, did not mark the row routed.
5. On the next 5-min tick, found the same row again (still empty `routed_to`),
   re-posted to Chat (duplicate), failed UPDATE again. Repeat until streaming
   buffer flush ~30 min later.

Net effect: every high/critical signal would generate ~6 duplicate Chat cards
in its first 30 minutes, exactly the window where the routing matrix is
supposed to deliver fast.

## Decision

**Move dispatch state off `triaged_items` into a new insert-only table
`agent_outputs.routed_events`.**

Schema:

| Column         | Type      | Mode     | Notes |
|----------------|-----------|----------|-------|
| `item_id`      | STRING    | REQUIRED | FK to `triaged_items.item_id` |
| `channel`      | STRING    | REQUIRED | matches `routing.matrix.Channel` enum values |
| `routed_at`    | TIMESTAMP | REQUIRED | partition column |
| `chat_status`  | INT64     | NULLABLE | HTTP status from the channel adapter; null for non-HTTP channels |
| `agent_run_id` | STRING    | NULLABLE | joins to `agent_audit_log.events.event_id` |

- Partitioned by DAY on `routed_at`, 730-day TTL (matches `triaged_items` per ADR 0024).
- Clustered on `item_id` for join-back lookups.
- Insert-only — fan-out writes via `insert_rows_json`, which is the same
  streaming-insert path the agent already uses. No DML, no streaming-buffer
  issue.
- `triaged_items.routed_to` column is **kept for one transitional release**
  but no longer written. Polling logic switches to a `LEFT JOIN routed_events`.
  Drop in a follow-up ADR once we've confirmed nothing reads it.

## Why insert-only events instead of switching to Storage Write API

The alternative — keep the column-on-row design but switch
`TriagedItemWriter` (and `AuditLogClient`) to the Storage Write API
COMMITTED stream — would also solve the problem (committed-stream rows
are immediately DML-able). It was rejected because:

- Roughly 100+ LOC of change touching two writers, with new protobuf glue
  and a new SDK dependency.
- Both writers would have to migrate together; partial migration leaves a
  mixed-mode failure surface.
- The event-sourced shape is the cleaner long-term fit anyway: each
  routing event is a real-world thing that happened at a real-world time,
  and recording it as its own row makes audit / replay straightforward.

The `routed_events` table is ~50 LOC of change, a single new TF resource,
and reuses the existing `insert_rows_json` plumbing the SA already has
permissions for.

## Consequences

**Positive**

- High/critical signals route in the first cycle after classification with
  no duplicate Chat cards.
- One row per (item, channel) dispatch — natural audit trail.
- Adding new channels (Gmail drafts in Tier 2, future workers) reuses the
  same insert-only pattern.

**Negative / accepted**

- One extra table in `agent_outputs`. Storage cost is negligible (~bytes per row).
- Polling SQL gets a `LEFT JOIN`. Cluster on `item_id` keeps this cheap
  even at scale; the lookback window is 30 min so the routed_events scan
  is tiny.
- `triaged_items.routed_to` becomes vestigial until removed in a follow-up.

## Rollout

1. Land the new table + code change + tests in one PR.
2. `terraform apply -target=module.agent_runtime.google_bigquery_table.routed_events`
   to create the table.
3. Push the new fan-out image (carries the code change).
4. Smoke: synthetic high-severity → expect one Chat card and one
   `routed_events` row, no duplicates.
5. Follow-up ADR / PR to drop `triaged_items.routed_to` once we've confirmed
   no consumers.
