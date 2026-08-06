# Materialized view for fast account/project owner lookups (ADR 0020).
#
# PRD §6.3: Triage uses this to resolve project owner email at classification
# time without round-tripping to Airtable. Refreshed daily — owner changes
# propagate within 24h, acceptable for v1.
#
# Single-base architecture: joins accounts → projects directly (the prior
# Clients middleman was retired in ADR 0020). The replica tables this view
# reads from are populated every 15 min by asb-airtable-sync (WRITE_TRUNCATE),
# so the view's refresh is decoupled from sync cadence.
#
# Columns also feed morning_brief + evening_reflection risk-flag readers,
# which join on (account_id, owner_email) and surface account_name in the
# digest body — added 2026-05-28 after the readers were silently 400-ing
# every day on a missing `account_name` column (caught/logged but tripping
# the post-2026-05-13 Cloud Run error alert).
#
# Schema-evolution gotcha: changing the materialized_view query triggers a
# replace. With deletion_protection=true on the existing live resource, the
# destroy step will fail. Workflow for this apply:
#   1. `bq rm -t -f agency-brain-demo:airtable_replica.account_owners_v`
#      (view only — no data loss, recomputed from accounts/projects)
#   2. `terraform apply -target=google_bigquery_table.account_owners_v`

resource "google_bigquery_table" "account_owners_v" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.airtable_replica.dataset_id
  table_id   = "account_owners_v"

  description = "Account → project → owner_email lookup for Triage Agent. PRD §6.3, ADR 0020."

  materialized_view {
    # The exposed `hipaa` column is sourced from `hipaa_excluded` — the
    # cascade-derived system column every other SQL surface gates on
    # (hipaa_filter_check.py contract) — NOT the raw Airtable checkbox.
    # The WHERE guards are defense-in-depth: HIPAA rows are already
    # excluded at sync time by filterByFormula, so they filter nothing
    # in the steady state, but they keep this view safe if the sync-time
    # filter ever gaps. Column name stays `hipaa` for downstream readers
    # (morning_brief RiskFlagsReader filters `ao.hipaa = FALSE`).
    query = <<-SQL
      SELECT
        a._airtable_record_id AS account_id,
        a.company_name        AS account_name,
        p._airtable_record_id AS project_id,
        p.owner               AS owner_email,
        COALESCE(a.hipaa_excluded, FALSE) AS hipaa
      FROM `${var.brain_project_id}.airtable_replica.accounts` a
      JOIN `${var.brain_project_id}.airtable_replica.projects` p
        ON a._airtable_record_id IN UNNEST(p.account)
      WHERE p.owner IS NOT NULL
        AND COALESCE(a.hipaa_excluded, FALSE) = FALSE
        AND COALESCE(p.hipaa_excluded, FALSE) = FALSE
    SQL
    # Non-incremental MV: BQ requires max_staleness (set at the table level)
    # instead of a scheduled refresh. Refresh happens at query time when data
    # is older than the threshold — cheaper than a daily rebuild for a small
    # ownership table that changes rarely. PRD §6.3 ~daily semantics preserved.
    allow_non_incremental_definition = true
  }

  max_staleness = "0-0 1 0:0:0" # 1 day (Y-M D H:M:S interval), table-level arg

  deletion_protection = true

  labels = {
    workstream = "ws-b"
    component  = "ownership-lookup"
  }
}
