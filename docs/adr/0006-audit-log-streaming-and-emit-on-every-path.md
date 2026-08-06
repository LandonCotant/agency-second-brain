# ADR 0006 — Audit log: streaming inserts + emit on every code path

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-C (Agent Runtime)

## Context

PRD §4.6 layer 2 mandates that every agent invocation writes a structured
row to `agent_audit_log.events`. PRD §1.5 (build goal #5) requires those
events be "queryable within seconds of the action." PRD §6.1 puts audit
emission in the base class so it's not optional per agent.

Three implementation choices weren't obvious from the PRD alone:

1. Streaming inserts (`insertAll`) vs. batched load jobs.
2. Whether the base class emits on every code path — including when `_run`
   raises — or only on successful invocations.
3. Where to record the "this needs human review" signal that PRD §6.1
   triggers when `confidence < 0.7`.

## Decision

**1. Streaming inserts.** `AuditLogClient.emit` calls
`bigquery.Client.insert_rows_json` for every event. No batching, no buffer.

**2. Emit on every code path.** `BaseAgent.invoke` emits a row before
returning success, before re-raising on exception, and before raising on
HIPAA pre-flight trip. There is no `try`/`except` in subclass `_run` that
can prevent emission, because emission happens in the base class wrapper.

**3. Human-review signal lives on the audit row.** When
`output.confidence < 0.7`, the audit row's `human_review_routed` column is
set to `true`. WS-D queries the audit log (joined with `agent_outputs.*`
once those tables land in PR #3) to compose routing decisions.

## Rationale

### Streaming inserts

- **Latency.** Streaming inserts are visible to queries within seconds.
  Load jobs batch on a Cloud Composer / Cloud Function cadence; that
  introduces minutes of lag for an event the routing layer (WS-D) and
  alerts (WS-E) react to in near-real-time.
- **Volume profile.** PRD §15 projects ~50k events/month. That's small
  enough that streaming insert costs (~$0.01/200MB) are negligible.
- **Operational simplicity.** No buffer to flush on shutdown, no batch
  job to monitor, no out-of-order events on retry.

The downside — streaming insert quota (per-second, per-table limits) —
is well above our event rate, and the BQ side surfaces 5xx / quota errors
that we propagate as `AuditLogWriteError` for the WS-E alert.

### Emit on every code path

If audit emission is only on success, three failure modes silently drop
rows: an unhandled exception in `_run`, a HIPAA pre-flight trip, and a
caller-side `try`/`except` around `invoke`. PRD §6.1 ("every invocation,
success or failure") and §1.5 ("every agent action is auditable") both
forbid this.

The base class owns the emission, not the subclass. A WS-G author cannot
"forget" to emit, because the only way to skip emission is to bypass
`BaseAgent.invoke` entirely — which is reviewed in PRs.

### `human_review_routed` on the audit row

The alternatives:

- **A separate `human_review_queue` table.** Adds a second write per
  flagged invocation, doubles the failure surface, and still requires the
  audit row to record that flagging happened (otherwise audit log + queue
  can drift). Net: more moving parts for no extra observability.
- **Computed downstream by the routing layer.** Forces WS-D to know the
  threshold (0.7), which is the agent runtime's policy — coupling that
  layers shouldn't have.

Carrying the boolean on the audit row keeps the policy centralized in the
base class and gives WS-D / WS-E a single source for "what got flagged."

## Consequences

- WS-E's alert "audit log write failure ≥ 1 event" (PRD §8.2) is the
  load-bearing detector: a streaming-insert failure is an
  `AuditLogWriteError` raised to the caller, *and* visible as a BQ
  side-channel error metric WS-E watches.
- The base class's emission failure handling re-raises rather than
  swallowing — agents never silently lose audit rows. A failed invocation
  surfaces to whatever scheduler / Pub/Sub subscriber dispatched it.
- PR review must reject any subclass that overrides `invoke` instead of
  `_run` (which would bypass the wrapper).

## Revisit if

- Agent invocation rate grows to where streaming insert costs matter
  (>10M events/month).
- A specific agent demonstrates a need for asynchronous emission (e.g.
  hard latency budget where a 50ms BQ insert is unacceptable). Solution
  would be a per-agent in-process queue with a flush-on-shutdown
  guarantee, not a global change.
- Human-review routing becomes more complex than a single boolean (e.g.
  multi-tier review). At that point a dedicated routing decision table
  may be warranted.
