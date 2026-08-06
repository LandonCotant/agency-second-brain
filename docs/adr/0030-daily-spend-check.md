# ADR 0029 — Daily spend check + per-project thresholds + manual shutoff

**Status:** Accepted
**Date:** 2026-05-02
**Workstream:** WS-F (security/audit) + cost guardrails

## Context

Following the 2026-05-02 cost incident (3 orphan Reasoning Engines billing
24/7, ~$40/day vs the $50/mo budget alert from ADR 0024), the operator
asked for a hard daily-spend threshold of $15/day on
`agency-brain-demo`. ADR 0028 added an alert on `CreateReasoningEngine`
events but only catches that specific failure mode. A general
"daily-spend exceeded" guardrail is the next layer.

GCP Billing Budgets are natively **monthly**: `calendar_period =
{MONTH,QUARTER,YEAR}` only; `custom_period` is one-shot, not recurring.
A true daily threshold requires querying spend ourselves.

The operator manages all 5 projects on billing account
`000000-000000-000000` and wants the check scoped to the three with
non-trivial spend potential. The other two (`asb-tfstate-bootstrap`,
`secondbrain-493921`) are visibility-only / archived.

## Decision

Add a 5th entry to the `audit_jobs` map in
`terraform/modules/security/runtime_audits.tf`:

- **Job:** `asb-audit-cost-daily-check`, Cloud Run Job, daily at 09:00 UTC.
- **SA:** `asb-audit-cost-daily` with custom role `tbAuditCostDailyCheck`
  (BigQuery jobs.create + dataViewer on `billing_export`).
- **Image:** reuses the existing `asb-audit:bootstrap` image; new entrypoint
  module `agency_brain.audit.daily_spend_check`.
- **Behavior:** queries `billing_export.gcp_billing_export_v1_*` for cost
  in the previous 24h grouped by project + service, restricted to the
  three monitored projects; posts a Chat card to the existing
  `Brain alerts` space via the `second-brain-gchat-webhook` secret with
  per-project totals; if any project exceeds its threshold, also emits a
  structured log event `COST_THRESHOLD_EXCEEDED` that a new log-based
  metric + alert policy escalate to Chat + owner email.

### Per-project thresholds (env-var configurable)

```
COST_THRESHOLDS_USD = {
  "agency-brain-demo": 15,
  "agency-mlops-hipaa":   15,
  "agency-mlops-dev":     30
}
```

Brain and HIPAA at $15 (production-like, tight). Dev at $30 (operator
flagged it has legitimate-spike potential during MLOps work).
Re-tuning is a single TF edit + targeted apply — no code change.

### Notify-only, manual shutoff

The user picked notification + manual shutoff over auto-shutoff. The
"shut down" path is `scripts/disable_billing.sh`, a one-shot script
that detaches the billing account from a project via
`gcloud beta billing projects unlink`. It prompts before acting and
prints the reattach command.

Why not auto-shutoff: auto-disabling billing requires a service
account with `billing.resourceAssociations.delete` on
billing-account `000000-000000-000000`. That billing account holds 5
projects across two unrelated workloads (Agency Second Brain, Agency MLOps);
granting *any* automated principal billing-account-scope delete power
is a meaningful blast-radius increase for marginal latency benefit
(humans react to a Chat ping in minutes, not days). The kill switch
stays a deliberate human action.

## Why piggyback on the audit framework instead of a new module

The existing `audit_jobs` map (ADR 0012) already provides: per-script
SA + custom role pattern, shared invoker SA, scheduler wiring,
`asb-audit:bootstrap` image with module-override command, BQ
`agent_audit_log.events` row emission via `DriftReporter`. Adding a
5th entry is one map row + one custom role + two BQ dataset bindings
+ one secret accessor binding — strictly additive, no new module.

The cost check is mechanically the same shape as a security audit
(scheduled BQ query, structured emit, threshold-driven alert) even if
the *concern* is cost rather than security. Spinning up a parallel
`cost_guardrails` module would duplicate the pattern for one job.
Revisit if cost-side jobs grow past two.

## Caveats

- **BQ billing export lag.** Google's docs note typical 6-24h delay
  before usage flows to the export tables. The job runs at 09:00 UTC
  and reports the previous calendar day's *finalized* cost — i.e. an
  incident detected at $40/day would surface ~24-36h after spend
  starts, not real-time. This is dramatically faster than the 3-day
  detection window of the original incident, but not real-time. For
  real-time we'd need streaming usage events (not exposed by GCP for
  public Cloud Billing).
- **First valid run is ~24h after operator enables export.** The
  Console step to enable billing export was performed 2026-05-02; the
  first day with complete data should be 2026-05-04 query window.
  Earlier runs will return empty / partial results and emit
  `COST_AUDIT_OK` with `note=billing_export_not_yet_populated`.
- **Reporter event IDs.** The existing `_reporter.AuditEventId` enum
  models security events. Cost adds two new ids:
  - `COST_AUDIT_OK` — daily run completed, all under thresholds.
  - `COST_THRESHOLD_EXCEEDED` — at least one project over its threshold.
  These are emitted via the same `DriftReporter` to keep the
  one-row-per-run contract intact.

## Consequences

- One additional Cloud Run Job execution per day (negligible cost,
  pennies/month at most).
- Daily Chat post in `Brain alerts`, regardless of breach. Operator
  builds intuition for normal spend bands. Tunable to weekly if noisy.
- Loud Chat + email alert on any threshold breach; auto-close after
  7 days (matches ADR 0008 strategy).
- The `second-brain-gchat-webhook` secret now has a 4th consumer
  (existing: routing fan-out, manual smoke); no rotation impact.
- `scripts/disable_billing.sh` exists as a documented kill switch.
  Re-enabling is `gcloud beta billing projects link <PROJECT>
  --billing-account=000000-000000-000000` — printed by the script for
  immediate copy/paste recovery.
