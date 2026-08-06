# AI Ops Dashboard reader — service account, custom IAM role, dataset
# and table bindings for the external personal AI Ops Dashboard.
#
# Context: the dashboard lives in a separate repo
# (/path/to/ai-ops-dashboard) and runs on
# the operator's Mac. It is NOT an agent — no Cloud Run job, no Pub/Sub
# subscription, no Gmail/Airtable writes. It pulls data via an SA key
# stored locally at ~/.config/gcloud/aiops-dashboard-key.json, or via
# ADC impersonation from the operator's account (see the token-creator
# binding below).
#
# Posture (least-privilege per PRD §4.2):
#   • Read-only (dataViewer) on agent_audit_log, agent_outputs,
#     airtable_replica, billing_export datasets.
#   • dataEditor on the SINGLE table agent_outputs.decisions so the
#     dashboard can transition drafted decisions to confirmed /
#     dismissed. Idempotency is application-side (WHERE status =
#     'drafted'); least-privilege is enforced here by scoping the
#     editor grant to one table.
#   • Drafts boundary (PRD §4.7) preserved — no write access to
#     triaged_items, risk_flags, routed_events, briefs, reflections,
#     notes, etc.
#
# Module placement: lives here rather than in security/ so it can
# directly reference google_bigquery_dataset.agent_outputs and
# google_bigquery_table.decisions without threading them through
# module outputs. Not an agent itself — purely a reader.
#
# Pattern mirrors triage_agent_iam.tf (custom role + dataset bindings).

resource "google_service_account" "tb_aiops_dashboard_sa" {
  project      = var.brain_project_id
  account_id   = "asb-aiops-dashboard-sa"
  display_name = "AI Ops Dashboard Reader"
  description  = "External personal AI Ops Dashboard (separate repo, Mac-hosted). Reads agent_audit_log, agent_outputs, airtable_replica, billing_export; updates status on agent_outputs.decisions only. No agent runtime."
}

resource "google_project_iam_custom_role" "tb_aiops_dashboard" {
  project     = var.brain_project_id
  role_id     = "tbAiopsDashboard"
  title       = "AI Ops Dashboard Reader"
  description = "Project-level perms for the AI Ops Dashboard reader. Data access granted separately via dataset/table IAM bindings."
  stage       = "GA"
  permissions = [
    # Run BQ queries. Data access scoped via dataset/table IAM below.
    "bigquery.jobs.create",
    "bigquery.datasets.get",
  ]
}

resource "google_project_iam_member" "tb_aiops_dashboard_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_aiops_dashboard.id
  member  = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

# Dataset-level dataViewer bindings — read-only.

resource "google_bigquery_dataset_iam_member" "tb_aiops_dashboard_audit_log_viewer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_aiops_dashboard_outputs_viewer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

# airtable_replica + billing_export datasets live outside this module;
# bind by string dataset_id (same pattern as
# triage_agent_iam.tf:69 for airtable_replica).

resource "google_bigquery_dataset_iam_member" "tb_aiops_dashboard_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_aiops_dashboard_billing_viewer" {
  project    = var.brain_project_id
  dataset_id = "billing_export"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

# Table-level dataEditor — ONLY agent_outputs.decisions. Dashboard
# moves drafted decisions to confirmed | dismissed via:
#   UPDATE agent_outputs.decisions SET status = ?
#   WHERE decision_id = ? AND status = 'drafted'
# The WHERE clause is application-side idempotency; least-privilege is
# enforced here by scoping the dataEditor grant to this one table.

resource "google_bigquery_table_iam_member" "tb_aiops_dashboard_decisions_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.decisions.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_aiops_dashboard_sa.email}"
}

# Allow the operator to impersonate the SA via ADC for local development —
# `gcloud config set auth/impersonate_service_account
# asb-aiops-dashboard-sa@<project>.iam.gserviceaccount.com` — so the
# raw SA key file isn't required day-to-day. Mirrors
# triage_token_creator_landon at triage_agent_iam.tf:95-99.
# Resource-scoped, not project-wide.
resource "google_service_account_iam_member" "aiops_dashboard_token_creator_landon" {
  service_account_id = google_service_account.tb_aiops_dashboard_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:owner@example.com"
}
