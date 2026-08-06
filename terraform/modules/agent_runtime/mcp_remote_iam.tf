# Remote brain MCP (Cloudflare Worker) — service account, custom IAM
# role, dataset/table bindings. ADR 0067.
#
# Context: the remote MCP server is a TypeScript Cloudflare Worker
# (workers/brain-mcp/) serving the 15 brain tools to claude.ai custom
# connectors (phone/web/desktop) behind OAuth 2.1. It is NOT an agent —
# no Cloud Run job, no scheduler, no Pub/Sub. Workers have no ambient
# GCP identity, so this SA's exported JSON key is stored ONLY as a
# Cloudflare Worker secret (90-day rotation,
# docs/runbooks/mcp-key-rotation.md). Never committed, never in
# Secret Manager — single copy.
#
# Posture (least-privilege per PRD §4.2, mirrors
# aiops_dashboard_reader_iam.tf):
#   • Read-only (dataViewer) on agent_outputs + airtable_replica.
#   • dataEditor scoped to the FIVE tables the write tools touch:
#     notes + notes_links (capture_note + wikilink edges per ADR 0053,
#     plus ADR 0052 synthetic companion rows), decisions
#     (insert_decision / mark_decision_status), wins (insert_win),
#     signal_feedback (record_feedback, ADR 0060).
#   • aiplatform.endpoints.predict for text-embedding calls
#     (brain_ask / capture_note).
#   • run.invoker on the asb-people-sync Job only (sync_people tool
#     fires the Jobs :run REST API).
#   • token-creator on asb-agent-triage-sa for Drive/Docs impersonation
#     (update_weekly_doc, ADR 0044 path). This expands the ADR 0064
#     impersonation graph by one member — accepted in ADR 0067 §3.
#   • Drafts boundary (PRD §4.7) preserved — no write access to
#     triaged_items, risk_flags, routed_events, briefs, reflections.

resource "google_service_account" "tb_mcp_sa" {
  project      = var.brain_project_id
  account_id   = "asb-mcp-sa"
  display_name = "Remote Brain MCP (Cloudflare Worker)"
  description  = "Remote MCP server on Cloudflare Workers (ADR 0067). Reads agent_outputs + airtable_replica; table-scoped writes only; fires asb-people-sync; impersonates triage SA for Drive. Key lives in Worker secrets only."
}

resource "google_project_iam_custom_role" "tb_mcp_remote" {
  project     = var.brain_project_id
  role_id     = "tbMcpRemote"
  title       = "Remote Brain MCP"
  description = "Project-level perms for the remote MCP Worker SA (ADR 0067). Data access granted separately via dataset/table IAM bindings."
  stage       = "GA"
  permissions = [
    # Run BQ queries. Data access scoped via dataset/table IAM below.
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # text-embedding-005 for brain_ask / capture_note.
    "aiplatform.endpoints.predict",
  ]
}

resource "google_project_iam_member" "tb_mcp_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_mcp_remote.id
  member  = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

# Dataset-level dataViewer — read tools span most tables in both
# datasets (client_summary alone JOINs accounts/projects/triaged_items/
# risk_flags/notes), so dataset-level viewer rather than per-table.

resource "google_bigquery_dataset_iam_member" "tb_mcp_outputs_viewer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

# airtable_replica lives outside this module; bind by string dataset_id
# (same pattern as aiops_dashboard_reader_iam.tf:77).
resource "google_bigquery_dataset_iam_member" "tb_mcp_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

# Table-scoped dataEditor — ONLY the five write-tool targets.

resource "google_bigquery_table_iam_member" "tb_mcp_notes_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.notes.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

resource "google_bigquery_table_iam_member" "tb_mcp_notes_links_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.notes_links.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

resource "google_bigquery_table_iam_member" "tb_mcp_decisions_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.decisions.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

resource "google_bigquery_table_iam_member" "tb_mcp_wins_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.wins.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

resource "google_bigquery_table_iam_member" "tb_mcp_signal_feedback_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = google_bigquery_table.signal_feedback.table_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

# sync_people tool — execute the asb-people-sync Job via the Cloud Run
# Jobs :run REST API. Job-scoped, mirrors people_sync.tf:168.
resource "google_cloud_run_v2_job_iam_member" "tb_mcp_people_sync_invoker" {
  project  = google_cloud_run_v2_job.tb_people_sync.project
  location = google_cloud_run_v2_job.tb_people_sync.location
  name     = google_cloud_run_v2_job.tb_people_sync.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}

# update_weekly_doc — Drive/Docs via impersonation of asb-agent-triage-sa
# (ADR 0044 folder-share path; the local stdio server uses the same
# mechanism via the operator's ADC). SA-resource-scoped, not
# project-wide. Expands the ADR 0064 graph by one — ADR 0067 §3.
resource "google_service_account_iam_member" "triage_token_creator_mcp" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_mcp_sa.email}"
}
