# Materialized view for the Triage Agent's sender → project resolver
# (ADR 0019, ADR 0020).
#
# Single-base architecture (ADR 0020). The view chain:
#
#   Gmail.sender → contacts.email
#                → contacts.account → accounts._airtable_record_id
#                → projects.account (REPEATED)
#                → team.workspace_email = projects.owner (LEFT JOIN, ADR 0019)
#
# The Team LEFT JOIN surfaces `owner_user_id` (the Airtable usrXXX
# collaborator id) so the Triage Agent can populate Tasks.Owner explicitly.
# LEFT so projects whose owner has no Team row don't drop out — those rows
# carry NULL owner_user_id and the agent omits Owner on POST.
#
# Day-1 hit rate is bounded by how many Contacts are populated. The
# resolver's no-match path (PR 4g) is the load-bearing one for v1; this
# view is the happy-path mechanism that engages as Contacts populate.
#
# HIPAA story: airtable_replica.accounts is already filtered at sync time
# (PRD §4.1 layer 2), so any account with HIPAA = TRUE is absent from the
# join. The view also carries `WHERE COALESCE(a.hipaa_excluded, FALSE) = FALSE`
# defensively, mirroring the canonical clause that hipaa_filter_check.py
# enforces on raw SQL files (PRD §4.8).

resource "google_bigquery_table" "sender_to_project_v" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.airtable_replica.dataset_id
  table_id   = "sender_to_project_v"

  description = "Sender-email → Operations Project lookup for the Triage Agent. Joins Contacts → Accounts → Projects (+ Team for owner_user_id), restricted to Active projects (ADR 0019, ADR 0020, PR 4g)."

  materialized_view {
    query = <<-SQL
      -- HIPAA-EXCLUDE
      SELECT
        LOWER(ct.email)                                       AS sender_email,
        LOWER(SPLIT(ct.email, '@')[SAFE_OFFSET(1)])           AS sender_domain,
        ct._airtable_record_id                                AS contact_id,
        a._airtable_record_id                                 AS account_id,
        p._airtable_record_id                                 AS project_id,
        p.owner                                               AS owner_email,
        t.user                                                AS owner_user_id,
        p.phase                                               AS project_phase,
        p.status                                              AS project_status,
        p._airtable_last_modified                             AS project_last_modified,
        COALESCE(a.hipaa_excluded, FALSE)                     AS hipaa_excluded
      FROM `${var.brain_project_id}.airtable_replica.contacts` ct
      JOIN `${var.brain_project_id}.airtable_replica.accounts` a
        ON a._airtable_record_id IN UNNEST(ct.account)
      JOIN `${var.brain_project_id}.airtable_replica.projects` p
        ON a._airtable_record_id IN UNNEST(p.account)
      LEFT JOIN `${var.brain_project_id}.airtable_replica.team` t
        ON LOWER(t.workspace_email) = LOWER(p.owner)
      WHERE ct.email IS NOT NULL
        AND p.status = 'Active'
        AND COALESCE(a.hipaa_excluded, FALSE) = FALSE
        AND COALESCE(p.hipaa_excluded, FALSE) = FALSE
    SQL
    # Non-incremental MV — same pattern as account_owners_v. Refresh happens
    # at query time when staleness exceeds max_staleness; cheaper than a
    # scheduled rebuild for a small lookup table that changes rarely.
    allow_non_incremental_definition = true
  }

  max_staleness = "0-0 1 0:0:0" # 1 day (Y-M D H:M:S interval)

  deletion_protection = true

  labels = {
    workstream = "ws-b"
    component  = "sender-resolver"
  }

  # The replica tables this view reads must exist before BigQuery
  # validates the materialized_view query — otherwise the first apply fails
  # with "Not found: Table" on the upstream tables.
  depends_on = [google_bigquery_table.replica]
}
