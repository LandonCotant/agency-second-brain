# Runbook — Cost guardrails setup

One-time operator steps for the cost-guardrail items that aren't TF-managed.
ADR 0024 has the rationale. The TF parts (AR cleanup policies, BQ
partition expiration) land via a normal targeted apply on the same PR.

## Prerequisites

- ADR 0024 PR merged.
- Targeted `terraform apply` of the AR + BQ changes complete.
- You are signed in to gcloud as `owner@example.com` with
  billing-account access (or have someone who is).

## $50/month billing budget alert

The budget alert lives at the billing-account level, not the project
level — Terraform-managing it would require granting the Cloud Build SA
billing-account permissions, more credential surface than this 2-person
tool needs for a one-time create. So we run it as the operator.

```bash
# 1. Resolve the billing account that agency-brain-demo is attached to.
BILLING_ACCOUNT=$(gcloud billing projects describe agency-brain-demo \
  --format='value(billingAccountName)' \
  | sed 's|.*/||')
echo "billing account: $BILLING_ACCOUNT"

# 2. Create the budget. Three thresholds (50/90/100%) email the
#    billing-account IAM admins (the operator) by default —
#    --enable-default-iam-recipients is on by default.
#
#    Note: --budget-amount accepts an int + 3-letter currency code
#    WITHOUT a decimal (e.g. 50USD, NOT 50.00USD — gcloud parser
#    rejects the decimal form).
gcloud billing budgets create \
  --billing-account="$BILLING_ACCOUNT" \
  --display-name="agency-brain-demo 50usd monthly guard" \
  --budget-amount=50USD \
  --filter-projects=projects/agency-brain-demo \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

### Why no Cloud Monitoring channel hookup

Adding `--notifications-rule-monitoring-notification-channels=...`
returns `INVALID_ARGUMENT` on this project today. The Cloud Billing
Budgets service identity needs `roles/monitoring.notificationChannelUser`
on the project to publish to a Monitoring channel, and that grant is
not in place. For a 2-person tool, the default IAM-recipients email
path is sufficient — the operator is the billing admin and gets the threshold
emails directly. If we ever want the Chat channel hookup, follow:
https://cloud.google.com/billing/docs/how-to/budgets-notification-recipients
to grant the budgets service identity the channel-user role, then
re-run `budgets update --notifications-rule-monitoring-notification-channels=...`.

## Verification

```bash
# Budget exists.
gcloud billing budgets list \
  --billing-account="$BILLING_ACCOUNT" \
  --filter='displayName~"agency-brain-demo"' \
  --format='table(displayName,amount.specifiedAmount.units,thresholdRules[].thresholdPercent.flatten())'
# Expected: one row, $50, thresholds 0.5 / 0.9 / 1.0.

# AR cleanup policies present (one-time post-apply check).
for repo in asb-sync asb-audit asb-agents; do
  echo "=== $repo ==="
  gcloud artifacts repositories describe $repo \
    --location=us-central1 \
    --project=agency-brain-demo \
    --format='value(cleanupPolicies)'
done

# BQ partition expiration set.
for tbl in agent_audit_log.events agent_outputs.triaged_items agent_outputs.risk_flags; do
  echo "=== $tbl ==="
  bq show --format=prettyjson agency-brain-demo:$tbl \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["timePartitioning"].get("expirationMs"))'
done
# Expected: 31536000000 for events, 63072000000 for the other two.
```

## Smoke test

Skip — the Chat channel is already proven deliverable by the HIPAA
isolation alert (ADR 0008, verified 2026-04-28). Re-firing the channel
for a budget that won't trip is wasted noise.

## Rotation / changes

- **Raising the threshold** (e.g., $50 → $100): `gcloud billing budgets
  update <BUDGET_ID> --budget-amount=100USD`. Look up `<BUDGET_ID>` from
  the verification command's output.
- **Adding more recipients**: re-run the `create` command (or `update`)
  with multiple `--notifications-rule-monitoring-notification-channels`.
- **Decommissioning**: `gcloud billing budgets delete <BUDGET_ID>` and
  remove this runbook.

## Why TF doesn't own this

`google_billing_budget` exists, but using it would require
`roles/billing.viewer` (at minimum) on the Cloud Build SA at the
billing-account level — a credential surface bump for a single one-time
resource. ADR 0024 explicitly chose the operator-managed path. If we
ever need to budget more than one project under the same billing
account, revisit this trade-off.

## Daily spend check (ADR 0030)

The monthly $50 budget above catches *trend*; daily-resolution detection
needs a separate path because GCP Billing Budgets are monthly only
(`calendar_period = MONTH/QUARTER/YEAR`; `custom_period` is one-shot,
not recurring).

**What's deployed (since 2026-05-02):**
- Cloud Billing export to BigQuery, dataset `billing_export` in
  `agency-brain-demo`. Configured via Console (no gcloud/TF API).
- Cloud Run Job `asb-audit-cost-daily-check` (5th audit job in
  `terraform/modules/security/runtime_audits.tf`). Scheduler
  `asb-audit-cost-daily-check-cron` runs daily at `0 9 * * *` UTC.
- Per-project thresholds in `terraform/modules/security/main.tf`
  (`var.cost_thresholds_usd`):
  - `agency-brain-demo`: $15
  - `agency-mlops-hipaa`: $15
  - `agency-mlops-dev`: $30
- Log-based metric `asb-cost-threshold-exceeded` + alert policy
  `Daily spend threshold exceeded` (in `terraform/modules/observability/alerts_cost.tf`)
  routes `COST_THRESHOLD_EXCEEDED` events to the existing `Brain alerts`
  Chat space + owner email.
- Manual kill switch at `scripts/disable_billing.sh`.

**Verify the daily check ran:**

```bash
# Latest execution + result
gcloud run jobs executions list --job=asb-audit-cost-daily-check \
  --region=us-central1 --project=agency-brain-demo --limit=3
```

```sql
-- Audit row written by the job (one per run)
SELECT timestamp, output
FROM `agency-brain-demo.agent_audit_log.events`
WHERE agent_id = 'audit-cost-daily-check'
ORDER BY timestamp DESC LIMIT 5
```

**Tune a threshold** (env var-driven, no code change):
1. Edit `cost_thresholds_usd` in `terraform/modules/security/main.tf`.
2. `terraform plan -target=module.security.google_cloud_run_v2_job.audit['cost_daily_check']`
3. `terraform apply -target=module.security.google_cloud_run_v2_job.audit['cost_daily_check']`
4. Next scheduled execution picks up the new value (env vars refresh at
   container start).

**Add a project to monitor:**
1. Add `<project_id> = <usd_threshold>` to `cost_thresholds_usd` in
   `terraform/modules/security/main.tf`.
2. (If the project is on a different billing account) update the
   billing export source — currently scoped to
   `000000-000000-000000`.
3. Apply as above.

**Caveat — BQ billing export lag:** Google's docs note 6-24h delay
before usage flows. The 09:00 UTC run reports the previous calendar
day's *finalized* cost — not real-time. If the export window is empty,
the script emits `COST_AUDIT_OK` with `note=billing_export_empty` (this
is the expected first-day-after-enable behavior, not a bug).

## Manual kill switch — `scripts/disable_billing.sh`

ADR 0030 keeps shutoff a deliberate human action rather than auto-
disabling on threshold breach. To use:

```bash
# Verify the right project first
gcloud beta billing projects describe agency-brain-demo \
  --format="yaml(billingAccountName,billingEnabled,projectId)"

# Run the kill switch — prompts for confirmation
bash scripts/disable_billing.sh agency-brain-demo
```

**Effect:** within ~5 min, every paid resource in the project halts.
Cloud Run Jobs / Schedulers / Cloud Functions / Reasoning Engines / BQ
jobs / Vertex AI / Secret Manager — all stop. State (BQ tables, GCS
objects, Pub/Sub messages) is RETAINED for ~30 days; after that GCP may
start GC'ing un-billable resources.

**Recovery:**
```bash
gcloud beta billing projects link <PROJECT_ID> \
  --billing-account=000000-000000-000000
```
The script prints this command on completion. After re-linking,
schedulers + Cloud Run Jobs resume on their next tick automatically.
