# ADR 0026 — Triage dedup: remove redundant bridge write + input_hash dedup + Pub/Sub exactly-once

**Status:** Accepted
**Date:** 2026-05-01
**Workstream:** WS-G1 (triage)

## Context

WS-D smoke on 2026-05-01 surfaced that every Triage classification produces
**2 rows** in `agent_outputs.triaged_items` for the same `source_event_ref`,
which then causes 2 Chat cards via the (now correctly idempotent) routing
fan-out. Investigation revealed three independent gaps stacked on top of
each other:

### Gap 1: Redundant bridge-side write (architectural)

`RemoteTriageAgent._run` in `bridge.py` calls `self._items_writer.write(...)`
**after** `engine.query(...)` returns. But `engine.query()` invokes the
RE-side `TriageQueryAgent` which wires up its own `TriagedItemWriter` (per
ADR 0019, full Airtable + BQ writer chain inside the RE) and writes a row
during `TriageAgent._run`. Result: every classification = 1 RE-side write
+ 1 bridge-side write = 2 rows.

This is leftover code from before ADR 0019 moved the full writer chain
inside the RE. The bridge's `items_writer` was never removed.

Evidence: bridge logs from 2026-05-01 17:51 show one bridge container
processing one message (`pulled=1 acked=1 outcomes=classified`), yet two
BQ rows landed at 17:51:31 and 17:51:32. No concurrency, just the
double-write.

### Gap 2: No writer-side dedup (defense)

Even with Gap 1 fixed, Pub/Sub at-least-once delivery means a redelivered
message (rare, but real) would call `engine.query()` again → second BQ
row + second Airtable Task. The `triaged_items.input_hash` column has
been part of the schema since day one, documented as "Dedup key —
re-classification of the same signal updates rather than duplicates"
(`writers.py:191`), but **nothing consults it** — `TriagedItemWriter.write`
unconditionally inserts.

### Gap 3: Pub/Sub at-least-once delivery (source)

`asb-triage-input-sub` is a normal Pub/Sub pull subscription. Pub/Sub's
default delivery semantics are at-least-once: a single publish can be
delivered multiple times if the consumer's ack is delayed past the
600s ack-deadline or if the Pub/Sub system retries internally.

## Decision

Fix all three gaps in one PR.

### 1. Remove the redundant bridge-side write

- `RemoteTriageAgent.__init__` no longer takes `items_writer`.
- `RemoteTriageAgent._run` no longer calls any writer — it just invokes
  the RE and returns the parsed output.
- `bridge.py:main()` no longer constructs a `TriagedItemWriter`.
- The bridge becomes: pull from Pub/Sub → invoke RE → ack on success or
  poison.

### 2. Input-hash dedup in the RE-side writer

- New `TriagedItemWriter.find_recent_by_hash(input_hash, window) -> str | None`
  method. Issues a BigQuery `SELECT ... WHERE input_hash = @hash AND
  triaged_at >= TIMESTAMP_SUB(NOW, INTERVAL @window MINUTE)` against
  `agent_outputs.triaged_items`. Returns the existing `item_id` or `None`.
  - Streaming-buffer SELECT is allowed (only DML wasn't — see ADR 0025);
    this works even on rows seconds old.
- `TriageAgent._run` (RE-side, in `triage_agent.py`) computes the hash
  immediately after parsing the LLM output, calls `find_recent_by_hash`,
  and:
  - **Hit**: skips the Airtable draft and the BQ insert. Sets
    `output.dedup_skipped = True` and `output.dedup_existing_item_id`
    on the returned `TriageOutput`. Emits a structured log
    `triage.dedup_skip` event for dashboards.
  - **Miss**: proceeds with the existing draft-then-write flow.
- `BaseAgent`'s standard audit row still fires. `_summarize_output`
  serializes the new dedup fields so the audit row clearly shows the
  dedup status.

The LLM call still happens on dedup-hit because the output is needed
for the audit row's `_summarize_output`. Wasted LLM cost is negligible
(gemini-2.5-flash, ~$0.0001/call) and dedup-hits should be rare.

### 3. Pub/Sub exactly-once delivery on `asb-triage-input-sub`

- TF: `enable_exactly_once_delivery = true` on `google_pubsub_subscription
  .tb_triage_input_sub`. This narrows the Pub/Sub-side dedup window from
  "best-effort" to "guaranteed within the ack-deadline". Combined with
  Gap 2, eliminates the duplicate-row failure mode end-to-end.
- No SDK change required — the existing `subscriber.pull` path works
  with exactly-once subscriptions; it just gives stronger guarantees.

## Configurability

Window for the input-hash dedup is `TRIAGE_DEDUP_WINDOW_HOURS`,
default **24** (1 day). The 24h default is the largest window that's
unlikely to over-suppress — it covers normal Pub/Sub redelivery
backoff (≤10 min), ad-hoc retries (≤1 hour), and manual smoke tests
without suppressing legitimate re-classifications of the same signal
across days. The env var lives on the RE deploy (per
`scripts/deploy_triage_re.py`) and on local test runs.

## Consequences

**Positive**
- Each real-world signal produces exactly 1 BQ row, 1 Airtable Task, 1 Chat card.
- Defense in depth: even if a future change reintroduces a duplicate
  invocation, the writer-side dedup catches it.
- Bridge becomes a thinner adapter — easier to reason about, less to
  test.

**Negative / accepted**
- One extra BQ SELECT per classification (clustered on `severity`/`source`,
  not on `input_hash`; cost is small at our row volume but worth measuring
  before scaling). If it shows up in the bill, add `input_hash` as a
  cluster column.
- LLM call still happens on dedup-hit. Cheap; not worth restructuring
  the flow to skip.
- `enable_exactly_once_delivery` slightly increases per-message cost
  on Pub/Sub. At our volume (handfuls of messages/day) it's nothing.

## Rollout

1. Land the PR with code + tests + this ADR.
2. `terraform apply -target=module.agent_runtime.google_pubsub_subscription.tb_triage_input_sub`
   (flips `enable_exactly_once_delivery`).
3. Build + push new `asb-triage-bridge` image (carries the
   architectural-fix code change).
4. `gcloud run jobs update asb-triage-bridge --image=...` to point at
   the new image.
5. Redeploy the RE via `python scripts/deploy_triage_re.py --update`
   (carries the dedup logic + the env var).
6. Smoke: publish the same synthetic Client A message twice within
   5 seconds. Expect: 1 BQ row, 1 Airtable Task, 1 routed_events row,
   1 Chat card. Re-publish 30 hours later → expect a fresh second row
   (window expired).
