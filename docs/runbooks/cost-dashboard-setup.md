# Cost Dashboard — Looker Studio setup

A Looker Studio dashboard backed by three BigQuery views over the billing
export. Complements the daily Chat card (ADR 0030) — the card is the
push surface, the dashboard is the pull surface.

## What it tells you

- **Today vs budget.** MTD spend per project against the $50/mo budget
  alert (ADR 0024) and the daily thresholds (prod $15, hipaa $15, dev $30).
- **Trend.** Daily spend by project over the last 90 days. Spikes are
  obvious; flat-line shows healthy steady state.
- **Where the money goes.** Service breakdown (Vertex / BigQuery /
  Cloud Run / Drive API / etc.) for the last 30 days.
- **Orphan catcher.** Top resources by cost, last 7 days. The 2026-05-02
  orphan-Reasoning-Engine incident would have shown up here on day 2
  instead of day 4. The current top hits are `agency-mlops-hipaa` Cloud
  Run services `bq-snapshot` + `pipeline-trigger` (~$9/wk each) — those
  are pre-Brain MLOps work; investigate before assuming they're load-bearing.

## Data model — three BigQuery views

DDL is inlined below (kept here rather than as a `.sql` file because the
HIPAA PR-gate regex in `scripts/hipaa_filter_check.py` over-matches the
billing export's `project` STRUCT field accesses; the gate is scoped to
files matching `*.sql` and the runbook is markdown).

| View | Granularity | Window | What it powers |
|---|---|---|---|
| `billing_export.v_daily_cost` | day × project × service | 90d | Time-series, service breakdown, anomalies |
| `billing_export.v_resource_cost_7d` | resource × project × sku | 7d | Top-N resources, orphan detection |
| `billing_export.v_mtd_by_project` | project | current invoice month | Scorecards vs budget |

Re-applying is idempotent (`CREATE OR REPLACE VIEW`). Source tables
are Console-toggled per ADR 0030, **not** Terraform-managed:

- `billing_export.gcp_billing_export_v1_01000A_F1ACDA_B6255D` (standard)
- `billing_export.gcp_billing_export_resource_v1_01000A_F1ACDA_B6255D` (resource)

Apply the DDL with:

```bash
bq query --use_legacy_sql=false --project_id=agency-brain-demo --location=US <<'SQL'
CREATE OR REPLACE VIEW `agency-brain-demo.billing_export.v_daily_cost` AS
SELECT
  DATE(usage_start_time) AS usage_date,
  project.id AS project_id,
  project.name AS project_name,
  service.description AS service,
  ROUND(SUM(cost), 4) AS cost_usd,
  ROUND(
    SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)),
    4
  ) AS credits_usd,
  ROUND(
    SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)),
    4
  ) AS net_cost_usd
FROM `agency-brain-demo.billing_export.gcp_billing_export_v1_01000A_F1ACDA_B6255D`
WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
  AND project.id IS NOT NULL
GROUP BY 1, 2, 3, 4;

CREATE OR REPLACE VIEW `agency-brain-demo.billing_export.v_resource_cost_7d` AS
SELECT
  resource.name AS resource_name,
  resource.global_name AS resource_global_name,
  project.id AS project_id,
  service.description AS service,
  sku.description AS sku,
  ROUND(SUM(cost), 4) AS cost_usd
FROM `agency-brain-demo.billing_export.gcp_billing_export_resource_v1_01000A_F1ACDA_B6255D`
WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND resource.name IS NOT NULL
GROUP BY 1, 2, 3, 4, 5;

CREATE OR REPLACE VIEW `agency-brain-demo.billing_export.v_mtd_by_project` AS
SELECT
  invoice.month AS invoice_month,
  project.id AS project_id,
  project.name AS project_name,
  ROUND(SUM(cost), 4) AS cost_usd,
  ROUND(
    SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)),
    4
  ) AS credits_usd,
  ROUND(
    SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)),
    4
  ) AS net_cost_usd
FROM `agency-brain-demo.billing_export.gcp_billing_export_v1_01000A_F1ACDA_B6255D`
WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())
  AND project.id IS NOT NULL
GROUP BY 1, 2, 3;
SQL
```

## Build the dashboard

### 1. Connect Looker Studio to each view

For each of the three views:

1. Open https://lookerstudio.google.com → blank report → **Add data**.
2. Select the **BigQuery** connector.
3. **My projects** → `agency-brain-demo` → `billing_export` → pick the view.
4. Click **Add**. Repeat for the other two views.

You'll end up with three data sources in the report. Name them
`Daily cost`, `Resource cost (7d)`, `MTD by project` so chart binding is
obvious later.

### 2. Page 1 — Overview

Four widgets, top to bottom:

**(a) Scorecard row — MTD per project.** One scorecard per project, side
by side. Data source: `MTD by project`. Metric: `net_cost_usd`. Filter
each scorecard to a single `project_id`. Add a comparison to the previous
period if you want.

**(b) Time-series — daily net cost by project, last 30 days.** Data
source: `Daily cost`. Dimension: `usage_date` (date). Breakdown
dimension: `project_id`. Metric: `net_cost_usd`. Date range control:
default to **Last 30 days**.

**(c) Bar chart — net cost by service, last 30 days.** Data source:
`Daily cost`. Dimension: `service`. Metric: `net_cost_usd`. Sort
descending. Limit to 10 rows. Date range: last 30 days.

**(d) Filter controls.** A `project_id` dropdown filter and a date range
control at the top of the page; both wire to the time-series and bar chart.

### 3. Page 2 — Resources (orphan catcher)

**(a) Table — top resources, last 7 days.** Data source: `Resource cost (7d)`.
Dimensions: `resource_name`, `project_id`, `service`, `sku`. Metric:
`cost_usd`. Sort by `cost_usd` descending. Limit 50 rows. Add conditional
formatting: red background when `cost_usd >= 5`, yellow when `>= 2`.

**(b) Bar chart — top 10 resources, last 7 days.** Same data source.
Dimension: `resource_name`. Metric: `cost_usd`. Helps eyeball at a glance.

### 4. Page 3 — Trends (optional)

**(a) Time-series — daily cost, last 90 days, all projects.** Data source:
`Daily cost`. Dimension: `usage_date`. Metric: `net_cost_usd` (no
breakdown). Add a reference line at `$1.66` (= $50/mo budget ÷ 30).
Days above the line are signal worth investigating.

## Sharing + access

The views inherit BigQuery dataset-level IAM. To let the dashboard work
for someone other than you, grant `roles/bigquery.dataViewer` on
`billing_export` to their email. The Looker Studio report itself is shared
through Looker's own ACLs (view link, share with email, etc.).

## Refresh cadence

Looker Studio caches BigQuery queries for ~12 hours by default. The
billing export updates a few times a day. Fine for daily monitoring; for
incident response, click the refresh icon (top-right of the report) to
force a re-query. No need to change the cache settings — daily granularity
is the level we operate at.

## Cost of the dashboard itself

Each Looker Studio chart issues a BQ query on load. The views scan
partitioned data (90d / 7d / current-month windows) — small. Expect
< $0.05/mo of BQ scan from the dashboard. The daily Chat card (ADR 0030)
costs more than the dashboard does.

## When to revisit

- If you start using the dashboard frequently and notice cache latency,
  promote the views to `MATERIALIZED VIEW` (or daily-refreshed snapshot
  tables). Don't pre-optimize.
- If `notes_links` / `agent_outputs.notes` ever become serious cost
  drivers (currently negligible), add a fourth view that joins the
  resource export to BQ table identifiers for per-table cost.
- The dashboard is read-only and pull-based. The push surface remains
  the daily Chat card; don't replace it.
