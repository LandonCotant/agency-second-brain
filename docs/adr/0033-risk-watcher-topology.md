# ADR 0033 — Risk Watcher topology + signal-as-data architecture

**Status:** Accepted
**Date:** 2026-05-04
**Workstream:** WS-G2 (Risk Watcher), Phase 1 (E-commerce profile)

## Context

PRD §6.4 calls for a "Risk Watcher" agent that scans per-client state
for trouble signals — silent-after-deliverable, ROAS drop,
acknowledgment gap, etc. — and writes structured flags to
`agent_outputs.risk_flags` (table already provisioned per ADR 0009).
The classifier output joins the same WS-D fan-out as triaged items, so
critical risk flags ping `Brain alerts` Chat and a Gmail draft via the
multi-channel `RoutingFanoutAgent` (ADR 0032). Per PRD §5.6 week 4,
the e-commerce profile ships first; local + agency-partner profiles
follow as separate PRs.

PRD §6.4 also pins a specific shape: **one base `RiskWatcher` class +
profile-per-segment subclasses, each subclass holding a list of
`Signal` objects with `evaluate(client_state) -> Optional[Flag]`.**
Signals are diffable, testable, and tunable independently. Memory Bank
holds per-client baselines (rolling ROAS, response latency) so the
agent has rolling context without a custom BQ state table.

This ADR pins the topology + the signal-as-data contract before any
e-commerce signals land.

## Decisions

### 1. One Cloud Run Job, one daily scheduler tick

Mirrors the WS-G3 / WS-G ingestor topology (ADR 0029, ADR 0031). The
Risk Watcher runs as `asb-risk-watcher-sa` (new), invoked by a
dedicated `asb-risk-watcher-invoker-sa` via OIDC every 06:00 Pacific so
fresh flags are visible to the 7:25am Morning Brief (ADR 0029) on the
same day.

**Daily, not 5-min.** Risk signals operate on rolling windows
(per-client baselines from Memory Bank, 8-week ROAS, business-day
acknowledgment gap). Sub-daily cadence buys nothing — a 30-second
acknowledgment gap is not a signal we model. Daily also caps Vertex
cost at one model call per active client per day worst case (PRD §15
budget posture).

**Rejected: Reasoning Engine.** Same posture as ADR 0029 §3 —
Vertex SDK direct, NOT a Reasoning Engine. Avoids the orphan-RE cost
incident (ADR 0028) and the `CreateReasoningEngine` audit alert.

### 2. Signal-as-data: profiles ARE lists of Signals

```python
class Signal(Protocol):
    name: str
    severity: Severity
    def evaluate(self, client_state: ClientState) -> Flag | None: ...

class EcommerceProfile:
    signals: tuple[Signal, ...] = (...)
```

Each `Signal` is a small, testable unit with a pure
`evaluate(client_state) -> Flag | None`. Profiles are tuples of these.
A new signal is one new file + one entry in the profile's `signals`
tuple — no orchestration changes.

This shape lets WS-G2b (local profile), WS-G2c (agency partner), and
WS-G2d (cross-cutting acknowledgment gap) each ship as separate PRs
without merge conflict against the e-commerce work.

### 3. Memory Bank for per-client baselines

PRD §6.4 picks Memory Bank over a custom BQ state table. Namespace
follows the standard convention from `common/memory_bank.py`:

- `risk-watcher/{account_id}/baseline` — rolling 8-week response
  latency, ROAS proxy, acknowledgment-rate baseline, etc.

Each `RiskWatcher._run` reads the baseline at tick-start, evaluates
signals, writes the updated baseline at tick-end. Memory Bank is the
HIPAA-isolated store per ADR 0006 — HIPAA-flagged accounts never
reach the Risk Watcher because of the upstream replica filter
(`hipaa_filters.py`), so no special namespace handling is needed.

`VertexMemoryBank` lands when Agent Engine ships (per
`common/memory_bank.py:88`). PR-A (skeleton) and PR-B (e-comm signals)
both run against `InMemoryMemoryBank` in tests; the production wiring
in `main.py` builds whichever is configured at deploy time.

### 4. risk_flags row shape — schema-canonical, dedup on (account_id, pattern_name, day)

Existing `agent_outputs.risk_flags` schema (provisioned per ADR 0009)
is canonical. The writer composes one row per fired Flag. Dedup is a
SELECT pre-check on `(account_id, pattern_name, DATE(flagged_at))` —
the same signal firing twice on the same day for the same account is
suppressed at insert time (audit-only). Mirrors ADR 0026 dedup posture
on `triaged_items`.

### 5. PR sequencing

- **PR-A (this PR): skeleton.**
  - ADR 0033 (this file).
  - `agents/risk_watcher/{models,base,ecommerce_profile,writer}.py` —
    contracts, base class, empty profile, BQ writer.
  - `asb-risk-watcher-sa` + `tbRiskWatcher` custom role + BQ
    dataset bindings.
  - **No Cloud Run Job, no scheduler, no Dockerfile, no image.**
    Those land with PR-B once there's actual code to run.
  - Unit tests for the base class + writer + models.

- **PR-B: e-commerce profile + Cloud Run Job.**
  - Implement signal classes (AcknowledgmentGap,
    SilentAfterDeliverable, ROAS-drop, CTR-drop, etc.) backed by
    `airtable_replica.risk_profiles` (segment = "E-commerce").
  - Memory Bank baseline read/write per signal.
  - `main.py` Cloud Run Job entrypoint.
  - `Dockerfile.risk-watcher` + `cloudbuild.risk-watcher.yaml`.
  - `risk_watcher.tf` adds the Cloud Run Job + scheduler.

- **PR-C: routing wiring.**
  - Extend `fanout_main.row_to_input` to also pull from `risk_flags`
    (LEFT JOIN against `routed_events` keyed `(flag_id, channel)`).
  - Risk flags fan out to Chat + Gmail draft via the existing
    channel adapters from ADR 0032.

- **PR-D, PR-E, PR-F (deferred):** WS-G2b local profile, WS-G2c
  agency partner profile, WS-G2d cross-cutting acknowledgment-gap
  detector. Each its own ADR if it needs new infra.

## Alternatives considered

- **Reasoning Engine deploy** — rejected per ADR 0029 §3 (cost +
  orphan-RE risk).
- **Per-signal Cloud Run Job** — too much ceremony for sub-second
  signal evaluation. One Job dispatches all signals across all
  active accounts in a tick.
- **Custom BQ baselines table** — rejected per PRD §6.4 ("Memory
  Bank usage… replaces the BigQuery table approach").
- **Sub-daily cadence** — rejected per §1 (signals operate on rolling
  windows; sub-daily latency is not load-bearing).
- **Bundle PR-A and PR-B** — possible but balloons review surface.
  Skeleton + IAM is plan-clean and reviewable on its own; signals
  are the substantive review.

## Consequences

### Positive

- Profile parallelism: WS-G2b/c/d can ship as independent PRs.
- Memory Bank as the only state store keeps the agent stateless from
  BQ's perspective — no custom checkpoint table, no migrations.
- Reuses ADR 0032's multi-channel routing — risk flags get Chat +
  Gmail drafts "for free" once PR-C wires the polling query.
- ADR 0028's orphan-RE risk is avoided by Vertex-SDK-direct.

### Negative / risks

- Memory Bank Vertex-side wiring is still PR #2-deferred per
  `common/memory_bank.py:88`. Until that lands, production runs
  cannot persist baselines across ticks. PR-B must guard this:
  either fall back to a deterministic baseline (rolling-window
  computed from BQ each tick) or block scheduler enable until the
  Memory Bank instance ships.
- E-comm signals depend on Vantage / Shopify federation tables that
  aren't yet provisioned (per WS-B PR-4 deferred). PR-B will pick
  the subset of signals that work today against `airtable_replica.*`
  and defer ROAS/CTR until the data is present.

## References

- ADR 0006 — BaseAgent audit contract; HIPAA short-circuit
- ADR 0009 — `agent_outputs` schema design (`risk_flags` columns)
- ADR 0019 — Cloud Run Job + scheduler topology pattern
- ADR 0023 / 0025 / 0032 — WS-D routing fan-out (PR-C reuses)
- ADR 0026 — input_hash / dedup posture
- ADR 0029 — Morning Brief topology (Vertex SDK direct, not RE)
- ADR 0031 — Notes ingestor (cadence rationale: weekly for
  reflection corpus, daily here for client-state signals)
- PRD §6.4 — Risk Watcher pattern (Signal + profile + Memory Bank)
- PRD §5.6 week 4 — milestone alignment

## Closeout addendum — PR-C routing wiring

PR-C extends `asb-routing-fanout` to fan out `agent_outputs.risk_flags`
alongside `triaged_items` on the same 5-min schedule. No new infra:
the same Cloud Run Job, same SA, same channel adapters; only the
polling layer learns about a second source.

Implementation summary:

- `routing.polling.build_risk_flags_poll_query` — parallel SQL
  builder that reads `risk_flags` LEFT JOIN
  `airtable_replica.accounts` (for the Gmail-draft subject line) and
  emits the same `routed_channels` ARRAY for per-channel dedup.
- `routing.fanout_main.risk_flag_row_to_input` — maps risk-flag
  columns to `FanoutInput`. `flag_id` is written into
  `routed_events.item_id` (a generic source-id column; no DDL
  change). `signal_evidence` + `reasoning` concatenate into the
  formatter's single `reasoning` field. `source = "risk_watcher"`
  keeps the Gmail adapter's threading guard (which gates on
  `source == "gmail"`) from accidentally threading into nothing.
  v1 leadership-only — owner-scoped routing (account_manager →
  owner email lookup) is a future PR.
- `routing.fanout_main.main` runs two ticks per scheduler firing:
  one for triaged_items (5-min lookback, unchanged) and one for
  risk_flags (24h lookback, new env `RISK_FLAGS_LOOKBACK_MINUTES`).
  The long lookback covers transient errors AND respects ADR 0023's
  09:00–16:00 PT Chat severity window — a flag fired at 06:00 PT is
  dispatched to Gmail draft immediately and to Chat once the
  09:00 PT window opens.

Dispatch matrix (unchanged from ADR 0032):

- `critical` → Chat + Gmail draft (always)
- `high` → Chat (09:00–16:00 PT only) + Gmail draft (always)

Tests: 99 routing unit tests pass (15 new across PR-C). The new SQL
dry-run-validates against prod BQ at 228 bytes upper bound.

Phase status post-PR-C:

- ✅ PR-A skeleton + IAM (PR #74)
- ✅ PR-B e-comm signals + Cloud Run Job + image
  (PRs #75 + #76 + #77, image `adr-0033-risk-watcher-v3` live since
  2026-05-04 14:26 UTC, scheduler `asb-risk-watcher-daily` ENABLED)
- ✅ PR-C routing wiring (this PR)

Currently 0 active E-commerce accounts, so neither the daily Risk
Watcher tick nor the routing fan-out's risk_flags pass have anything
to dispatch. The first real flag will land in Chat + Gmail draft
within ~5 min of the next Risk Watcher tick after an E-comm client
is added.
