# ADR 0034 — Risk Watcher follow-up profiles: Local Service shipped, Agency Partner skeleton, cross-cutting Ack Gap deferred

**Status:** Accepted
**Date:** 2026-05-04
**Workstream:** WS-G2 (Risk Watcher), follow-up phases — PRD §5.6 week 5

## Context

ADR 0033 shipped the WS-G2 Risk Watcher topology and the e-commerce
profile (PR-A skeleton, PR-B signals, PR-C routing wiring). Per ADR
0033 §5, three follow-ups were deferred to separate PRs: WS-G2b (Local
Service profile), WS-G2c (Agency Partner profile), and WS-G2d
(cross-cutting Acknowledgment Gap detector). PRD §5.6 week 5 calls for
all three to ship in the same milestone window so the founder gets
profile coverage across the active client mix.

The honest data reality at this point in the build:

- **Vantage / Shopify / Google Business Profile federation tables are
  not yet provisioned** (WS-B PR-4 deferred; ADR 0033 §Consequences
  flagged this).
- **Active accounts in Operations:** 3 — Client C Studio, Client A,
  the agency sentinel. Of these only Client A plausibly fits
  one of the three new profiles (Local Service).
- **Of the 13 spec'd signals across b/c/d, only 2 are implementable
  against today's data sources** (`airtable_replica.*`,
  `agent_outputs.triaged_items`, Calendar via the existing DWD
  `calendar.readonly` allowlist entry).

This ADR pins the pragmatic shape: ship what the data supports,
structurally wire what it doesn't, and explicitly defer what would
overlap with existing detectors if we shipped a degraded version.

## Decisions

### 1. One Cloud Run Job, multi-segment loop

The existing `asb-risk-watcher` Job (ADR 0033) iterates over the three
segments per tick:

```python
for segment, profile_factory, state_loader_cls in [
    (Segment.ECOMMERCE,      build_ecommerce_profile,      EcommerceClientStateLoader),
    (Segment.LOCAL_SERVICE,  build_local_service_profile,  LocalServiceClientStateLoader),
    (Segment.AGENCY_PARTNER, build_agency_partner_profile, AgencyPartnerClientStateLoader),
]:
    thresholds = thresholds_loader.load(segment)
    profile = profile_factory(thresholds)
    if not profile.signals:
        log.info("risk_watcher.skipped segment=%s reason=no_signals", segment.value)
        continue
    states = state_loader_cls(...).load()
    output = watcher.invoke(RiskWatcherInput(states=states))
    # write flags as today
```

Profiles whose `Profile.signals` tuple is empty (Agency Partner v1)
short-circuit cleanly. Per-segment audit rows surface segment labels
in the existing `_summarize_input` output. Zero new SAs, schedulers,
or TF resources needed for the loop change.

**Rejected: three Cloud Run Jobs (one per segment).** Right shape
*if/when* one segment's tick takes minutes, has its own retry posture,
or needs an independent cron. None applies today (0 active E-comm + 1
LS candidate + 0 active AP). Three Jobs is enterprise ceremony for a
2-person agency — splitting later is a 30-line refactor.

**Rejected: env-var-driven segment fan-out.** Same as the loop with
extra knobs. Skip the env var; iterate the enum directly.

### 2. AcknowledgmentGapSignal parametrized for cross-segment reuse

The e-comm `AcknowledgmentGapSignal` already implements the rule "a
Triage-drafted task overdue past N business days fires." The Local
Service "Approval Slowdown" signal is the same rule with a different
label. Lift `pattern_name`, `segment`, and `severity` to constructor
params with E-comm defaults preserved:

```python
class AcknowledgmentGapSignal:
    def __init__(
        self,
        *,
        business_days_threshold: int = 5,
        pattern_name: str = "Acknowledgment Gap",
        segment: Segment = Segment.ECOMMERCE,
        severity: Severity = Severity.HIGH,
        ...
    ) -> None: ...
```

`build_local_service_profile()` constructs
`AcknowledgmentGapSignal(pattern_name="Approval Slowdown",
segment=Segment.LOCAL_SERVICE, severity=Severity.HIGH)`. Existing
e-comm tests are unaffected because the defaults match the prior
behavior.

**Rejected: per-segment subclass (`ApprovalSlowdownSignal extends
AcknowledgmentGapSignal`).** That's the enterprise-ceremony shape —
the rule is identical, only the labels differ. One class, two
configurations.

### 3. Loader package split

`src/agency_brain/agents/risk_watcher/loaders.py` (250 lines)
becomes a package:

```
loaders/
  __init__.py        # re-exports for API compatibility
  thresholds.py      # extracted RiskProfileThresholdsLoader (shared)
  ecommerce.py       # extracted EcommerceClientStateLoader
  local_service.py   # NEW — Approval Slowdown + Owner Disengagement
  agency_partner.py  # NEW — empty skeleton until Vantage lands
```

The `loaders/__init__.py` re-exports preserve every existing import
path (`from ...loaders import EcommerceClientStateLoader,
RiskProfileThresholdsLoader`). Tests, `main.py`, and any future
consumer change only if they want to reach for the new loaders.

**Justification:** the existing single-file shape would hit ~500
lines after PR-D and PR-E land. Doing the split now, before the new
loaders are added, costs nothing extra and avoids a refactor in the
PR-E review.

### 4. DWD `calendar.readonly` reuse for `asb-risk-watcher-sa`

Owner Disengagement (Local Service) needs Calendar data — has the
agency owner (the operator) been on meetings with attendees from the
client's contact domain in the last threshold window?

`calendar.readonly` is already on the DWD scope allowlist (ADR 0027
amended + ADR 0029 §2). `asb-agent-triage-sa` is the only
DWD-grantable SA per ADR 0027 §2. The pattern from ADR 0032 §4 (where
`asb-routing-sa` impersonates `asb-agent-triage-sa` for `gmail.compose`)
applies here: grant `asb-risk-watcher-sa`
`roles/iam.serviceAccountTokenCreator` on `asb-agent-triage-sa`
resource-scoped, then mint `calendar.readonly` credentials with
`subject="owner@example.com"` at runtime via
`common/dwd.py::DWDServiceFactory`.

**ADR 0027 §2 invariant preserved.** `asb-risk-watcher-sa` is now an
indirect impersonator (like `asb-routing-sa`); it is not added to the
DWD allowlist or granted any new OAuth scope. Workspace admin's DWD
grant continues to apply only to `asb-agent-triage-sa`'s client_id.

**No `docs/dwd_scopes.md` change.** The doc-driven allowlist already
lists `calendar.readonly`; the audit-side `drafts_boundary_check.py`
asserts the allowlist matches the live DWD config and does not gate
on which workload SA reaches for the scope. PR-gate
`drafts_static_check.py` blocks Gmail send/modify call sites in
`src/`; risk-watcher uses Calendar, which is unaffected.

### 5. Per-profile signal scope (honest accounting)

#### WS-G2b Local Service — implementable today

| Signal | Status | Threshold key |
|---|---|---|
| **Approval Slowdown** (HIGH) | Live in PR-D — reuses `AcknowledgmentGapSignal` against the LS segment. | `risk_profiles` row `(Local Service, Approval Slowdown)` |
| **Owner Disengagement** (CRITICAL) | Live in PR-D — Calendar attendance heuristic via DWD reuse. Threshold = days since last meeting with any attendee whose email domain matches the account's contact domains. | `risk_profiles` row `(Local Service, Owner Disengagement)` |
| GBP Decline (MEDIUM) | Deferred — needs `vantage_replica.gbp_metrics` (WS-B PR-4). |
| Ack Gap (GBP-flavored) (HIGH) | Deferred — same Vantage dependency. The general task-side Ack Gap is already covered by Approval Slowdown. |

#### WS-G2c Agency Partner — skeleton only

All five signals (Raw Data Inquiry, Refinement Silence, Report Volume
Decline, Refinement Ratio Drop, Ack Gap on end-client roster) need
Vantage report-access / refinement-Q logs that aren't provisioned.
PR-E ships the segment wiring and an empty profile so the multi-
segment loop scaffolding in `main.py` is already in place when
Vantage tables land. Inline comments in
`loaders/agency_partner.py` enumerate the deferred queries with their
target table names so the post-Vantage PR fills them in by deletion-
of-comments.

#### WS-G2d cross-cutting Acknowledgment Gap — deferred entirely

The unique G2d contribution is "Vantage KPI shifted AND comms
silence" — the *pairing* is what makes it the highest-leverage churn
predictor per spec §6.4. Both halves are gated:

- **KPI half** needs Vantage federation tables (deferred).
- **Comms-silence half** is already covered by per-profile
  `AcknowledgmentGapSignal` against drafted Tasks and by
  `SilentAfterDeliverableSignal` against `triaged_items`.

Shipping a degraded G2d that uses only the comms-silence half would
duplicate flags against accounts that already trip the per-profile
detectors, hurting signal:noise without adding the cross-cutting
value. PR-F is therefore deferred entirely; reopen when Vantage
federation lands and the KPI side becomes implementable.

### 6. Memory Bank baselines: capability preserved, not exercised in v1

`RiskWatcher.read_baseline` / `write_baseline` (`base.py:103-114`)
remain available. Neither v1 LS signal needs a rolling baseline —
both are static-threshold rules read from `risk_profiles`. When a
signal that genuinely needs a rolling baseline lands (e.g., a future
Vantage-fed refinement-ratio signal in PR-G), the BQ-per-tick
computation lives in the loader as a CTE next to the rest of that
loader's queries, with `RiskWatcher.write_baseline` cached for the
post-`VertexMemoryBank`-ship swap.

**Per the user direction:** when baselines are needed, compute from
BQ at tick-start; persist nothing for v1. The Memory-Bank-shaped
interface stays so the swap is one line later.

## Alternatives considered

- **Three Cloud Run Jobs / three schedulers** — see §1 rejection. The
  one-Job-multi-segment loop is the right shape until per-segment
  cadence diverges.
- **Inlining the new loaders into `loaders.py`** — cheapest now;
  causes a forced refactor in PR-E. Splitting once is cheaper than
  splitting twice.
- **Subclassing `AcknowledgmentGapSignal` per segment** — see §2
  rejection. The rule is identical.
- **Ship a degraded G2d using only the comms-silence half** — see §5
  rejection. Duplicate-flag risk against the per-profile detectors.
- **Hold all three follow-ups until Vantage lands** — kills the week-5
  milestone with no Vantage ETA. Shipping LS exercises the
  multi-segment topology and gives Client A active coverage.

## Consequences

### Positive

- Multi-segment scaffolding lands ahead of the data, so PR-G (Vantage-
  fed signals) becomes content-only — no orchestration changes.
- Client A gets active risk coverage via Approval Slowdown + Owner
  Disengagement on the same cadence E-comm uses today.
- Reuses the multi-channel routing fan-out from ADR 0032 — LS flags
  fan out to Chat + Gmail draft for free; zero routing changes.
- ADR 0027 §2 invariant (one DWD-grantable SA) preserved. The blast-
  radius story for DWD remains unchanged: `asb-agent-triage-sa` is
  still the only SA Workspace admin grants DWD on.

### Negative / risks

- **Single-source Calendar dependency.** Owner Disengagement quietly
  returns "no signal" if the Calendar API call fails. The signal is
  designed to fire when the meeting count is zero, so a Calendar API
  outage looks identical to "no meetings" at evaluate-time. Mitigation:
  the loader logs a warning on Calendar failure and the audit row's
  `output` summary distinguishes a call-failed tick from a quiet tick
  (`extras["calendar_api_status"]`).
- **Vantage-blocked signals are real coverage gaps.** GBP Decline,
  Refinement Silence, Raw Data Inquiry, etc. would each catch real
  client-risk patterns the v1 LS+E-comm coverage misses. The deferral
  ledger above is the source of truth; no signal is silently dropped.
- **AP profile is structurally live but functionally empty.** The
  scheduler ticks emit a `risk_watcher.skipped segment=Agency Partner`
  audit row; that's intentionally distinct from "no AP accounts" so
  the operator knows the profile is in-scope but waiting on Vantage.

## References

- ADR 0006 — BaseAgent audit contract; HIPAA short-circuit
- ADR 0009 — `agent_outputs.risk_flags` schema
- ADR 0019 — Cloud Run Job + scheduler topology pattern
- ADR 0023 / 0025 / 0032 — WS-D routing fan-out (LS flags reuse)
- ADR 0027 amended — DWD scope allowlist (`{gmail.compose,
  calendar.readonly}`); single DWD-grantable SA invariant
- ADR 0029 — Morning Brief Calendar reader (pattern reused)
- ADR 0032 §4 — `serviceAccountTokenCreator`-on-SA-resource binding
- ADR 0033 — Risk Watcher topology + signal-as-data architecture
- PRD §5.6 week 5 — milestone alignment
- PRD §6.4 — Risk Watcher pattern (Signal + profile + Memory Bank)
- spec §6.2 — Local Service profile signal definitions
- spec §6.3 — Agency Partner profile signal definitions
- spec §6.4 — Cross-cutting Acknowledgment Gap design (the G2d
  rationale)

## Closeout addendum — PR-F deferred ledger

PR-F (cross-cutting Acknowledgment Gap detector) is deferred under
this ADR. Reopen when:

1. WS-B PR-4 provisions `vantage_replica.*` federation tables, AND
2. The first vertical's KPI table (likely
   `vantage_replica.gbp_metrics` for LS or `vantage_replica.roas` for
   E-comm) carries enough history for a baseline diff to be
   meaningful (≥ 8 weeks per ADR 0033 §3).

The reopen PR's scope: a new `agents/risk_watcher/cross_cutting/`
subpackage holding the cross-segment Ack Gap detector, run on the
same daily tick after the per-segment passes complete. Output rows
carry `segment = "Cross-Cutting"` (a fourth Segment enum value) so
routing fan-out keeps its segment-agnostic posture.
