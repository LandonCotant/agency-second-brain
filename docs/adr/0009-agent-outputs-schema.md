# ADR 0009 — `agent_outputs` schema discipline

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-C (Agent Runtime)

## Context

WS-C PR #3 introduces four BigQuery tables under a new `agent_outputs`
dataset:

- `triaged_items` — Triage Agent's per-signal classification
  (PRD §6.1, spec §5.1 / §7).
- `risk_flags` — Risk Watcher's per-pattern detections
  (spec §5.2 / §6).
- `goals` — Goals state read by Triage via Cached Contents
  (PRD §6.3, spec §3.1, goal_hierarchy_v0.1.md).
- `goal_scores` — Goal Steward's weekly self-assessment time series
  (spec §6.1, goal_hierarchy weekly cadence).

Three decisions weren't obvious from the PRD or spec:

1. Whether all four tables share one dataset or split by canonicality.
2. Where Goal Steward writes (BQ-canonical vs. Airtable-canonical).
3. How to keep agent-emitted rows joinable to `agent_audit_log.events`
   without a second write path or a separate queue.

## Decision

**1. One dataset, mixed canonicality.** All four tables live under
`agent_outputs`. `triaged_items` and `risk_flags` are **BQ-canonical**:
the agent writes the structured row to BQ first, then materializes an
Airtable Tasks row (`Source = "Triage Agent" | "Risk Watcher"` per
`airtable/schema.json`) as a side-effect for human review. `goals` and
`goal_scores` are **Airtable-canonical**: humans edit in Airtable Goals
/ Goal Scores; WS-B PR-2's sync populates the BQ rows.

**2. Goal Steward writes Airtable, not BQ.** WS-G6 modifies Airtable
records (Status, Last Reviewed, scores). WS-B PR-2 mirrors those
changes into `agent_outputs.{goals, goal_scores}` so Triage's Cached
Contents (`status="Active" AND horizon IN ("Quarterly","1-year")`,
PRD §6.3) hits BQ instead of round-tripping Airtable.

**3. Every agent-written row carries `agent_run_id`.** The column joins
to `agent_audit_log.events.event_id`. It is REQUIRED on the
BQ-canonical tables (`triaged_items`, `risk_flags`) and NULLABLE on the
Airtable-replica tables (`goals`, `goal_scores`) — null when the
upstream Airtable write was a UI edit, non-null when Goal Steward wrote
directly.

## Rationale

### One dataset, mixed canonicality

The dataset boundary should reflect *what reads this*, not *where it
came from*. All four tables are read during agent inference: Triage
reads `goals` (Cached Contents) and `triaged_items` (its own history);
Risk Watcher reads `triaged_items` (rolling 30-day window per spec §6);
Morning Brief / Weekly Synthesizer read `triaged_items` + `risk_flags`
+ `goal_scores`. Splitting `goals` / `goal_scores` into
`airtable_replica` would force every agent into cross-dataset queries
and complicate Cached Contents lookups.

The alternative — one BQ-canonical dataset and a separate
Airtable-replica dataset — would require Goal Steward to dual-write
(BQ + Airtable) which is the failure mode this decision exists to
avoid: BQ writes that are not visible in the human UI until a sync
catches up, and Airtable edits that the agent runtime can't see.

### Goal Steward writes Airtable, not BQ

Humans need an editing surface today and Airtable is it. Making BQ
canonical would either (a) require a Brain-side admin UI we don't
have, or (b) leave Airtable as a stale display and drift the moment
anyone edits there. WS-B PR-2's sync direction (Airtable → BQ) is
already the established pattern for `airtable_replica.*`; reusing it
for `agent_outputs.{goals, goal_scores}` keeps one sync engine.

### `agent_run_id` everywhere

Every BQ-canonical row links back to its audit event so incident
review can ask "which Triage Agent run produced this misrouted item"
in a single JOIN. The replica tables make it NULLABLE because
Airtable-UI edits have no audit event — but a Goal Steward direct
write (rare, e.g. an automated quarterly carry-over) still carries
the link.

A separate "agent → output" mapping table was rejected: it doubles
the writes per invocation and introduces a third place the routing
layer has to consult. The audit table already records every
invocation; the output table records the structured result; the join
key is the only thing that connects them and it's cheap to carry.

## Two contract details that aren't obvious from the schema alone

### `triaged_items.positive_goal_achieving` is NULLABLE, not REQUIRED

Spec §7.1 (Axis 1) routes non-actionable signals to Trash / File / Tickle —
those signals never reach Axis 2 (the PGA dimension), so there's no PGA
strength to record. The column is therefore NULLABLE: WS-G1 writes
`strong | moderate | weak` when `actionable=true`, and NULL when
`actionable=false`. WS-D's routing matrix should treat NULL the same way
it treats Axis-1 non-actionable: don't promote to a Task, don't fan out.

Earlier draft made the column REQUIRED with a `none` sentinel. Rejected
because it forces every reviewer to remember which sentinel applies and
spec §7.2 doesn't define a value for the non-actionable case.

### `risk_flags.human_review_routed` mirrors `triaged_items` and the audit log

Both `triaged_items` and `risk_flags` carry a REQUIRED `human_review_routed`
BOOL that's set to `confidence < 0.7` per PRD §6.1. WS-D reads this column
directly without joining to `agent_audit_log.events`, so the routing query
stays cheap and the contract is consistent across all output tables.

The audit log row is the canonical record of the invocation; the output
table column is a denormalized copy for query cost. They must agree —
WS-C's BaseAgent enforces this by writing both from the same in-memory
value.

## Partition + cluster choices

| Table | Partition | Cluster | Why |
|---|---|---|---|
| `triaged_items` | `triaged_at` DAY | `severity, source` | Hourly digests + WS-D routing scan recent windows; severity + source are the two filters every dashboard uses. |
| `risk_flags` | `flagged_at` DAY | `client_id, severity` | "Open Critical flags for client X" is the most common query; per-client rolling baselines need fast client_id filters. |
| `goals` | none | `status, horizon` | ~30–100 rows total per goal_hierarchy_v0.1.md — partitioning would create mostly-empty partitions; cluster matches the Cached-Contents filter. |
| `goal_scores` | `week_of` MONTH | `goal_id` | ~12 active goals × 52 weeks ≈ 600 rows/yr. DAY would leave each partition with one row; MONTH preserves trend-query efficiency. |

`deletion_protection = true` on every table — these are operational
data the routing layer and historical analyses depend on; an
accidental `terraform destroy` of one is a multi-week recovery.

No partition expiration: outputs are kept indefinitely for trend and
incident review (matches the `agent_audit_log.events` stance).

## Consequences

- **WS-B PR-2** owns the write path for `goals` + `goal_scores`. It
  must populate `last_synced_at` on every refresh and respect the
  REQUIRED columns (the schema enforces presence; the sync code has
  to fail loudly on missing fields rather than dropping rows).
- **WS-G1 (Triage Agent)** writes `triaged_items` then materializes
  the Airtable Tasks row. If Airtable materialization fails, the BQ
  row is still authoritative and `airtable_task_record_id` stays
  NULL until a retry succeeds.
- **WS-G2 (Risk Watcher)** writes `risk_flags` (with full
  `signal_evidence` + `baseline_snapshot` JSON) then materializes the
  Airtable Tasks row. Same retry semantics.
- **WS-G6 (Goal Steward)** writes Airtable Goals / Goal Scores via
  the Airtable API. It does not write BQ directly except in the rare
  carry-over path (which sets `agent_run_id` so the row is
  attributable).
- **WS-D (routing)** reads `triaged_items` for severity → channel
  mapping (spec §8.1). It joins `agent_audit_log.events` on
  `agent_run_id` for cost / latency / model_armor_findings context.
- **WS-G SAs grant themselves `bigquery.dataEditor`** on the specific
  tables they write — this PR creates no SAs and no IAM. The
  hardened `least_privilege_check.py` (WS-F PR #4) will reject any
  later PR that tries to grant `*admin` on `agent_outputs`.

## Revisit if

- A new agent's output doesn't fit cleanly into one of the four
  tables — at that point evaluate whether to add a fifth table or
  whether the new agent should write into `triaged_items` with a
  `source` value.
- Goal Steward starts producing volume that justifies BQ-canonical
  goals (e.g. an automated weekly retrospective record per goal).
- BigQuery introduces native ENUM types — the STRING enums we use
  today would migrate cleanly with column-level type changes.
