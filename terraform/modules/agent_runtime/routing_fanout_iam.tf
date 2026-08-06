# WS-D Chat fan-out — service account, custom role, BQ dataset bindings (ADR 0023).
# PRD §6.5 (routing) + §4.2 (least-privilege IAM, no predefined *.admin /
# editor / owner — `scripts/least_privilege_check.py` enforces).
#
# Patterns mirrored from:
# - terraform/modules/agent_runtime/triage_agent_iam.tf (per-agent SA + custom role)
#
# Webhook delivery is IAM-invisible — no Workspace OAuth scope, no
# `chat.spaces.write` role binding. The Chat call is a plain HTTPS POST
# whose authorization is the URL itself (in Secret Manager). See ADR 0023
# for why webhook-only is acceptable for the audience-of-one Brain alerts
# space.

resource "google_service_account" "tb_routing_sa" {
  project      = var.brain_project_id
  account_id   = "asb-routing-sa"
  display_name = "Agency WS-D routing fan-out"
  description  = "Runs the WS-D Chat fan-out worker (ADR 0023). Reads agent_outputs.triaged_items, posts to Brain alerts via webhook (Secret Manager), inserts dispatch records into agent_outputs.routed_events (ADR 0025). No Gmail/Chat OAuth scope per PRD §4.7."
}

resource "google_project_iam_custom_role" "tb_routing_fanout" {
  project     = var.brain_project_id
  role_id     = "tbRoutingFanout"
  title       = "Agency WS-D routing fan-out"
  description = "Project-level permissions for asb-routing-sa. BQ dataset access is granted separately via google_bigquery_dataset_iam_member."
  stage       = "GA"
  permissions = [
    # BigQuery query + UPDATE execution. Data access scoped via dataset
    # IAM below; no project-level data permissions here.
    "bigquery.jobs.create",
    "bigquery.datasets.get",
  ]
}

resource "google_project_iam_member" "tb_routing_fanout_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_routing_fanout.id
  member  = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}

# Read + INSERT into agent_outputs.routed_events (the dispatch log per ADR 0025).
# Reading triaged_items is safe re: HIPAA — those rows are post-classification;
# the Triage Agent's HIPAA pre-flight has already filtered hipaa_excluded inputs.
resource "google_bigquery_dataset_iam_member" "tb_routing_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}

# Write audit events via BaseAgent (one row per dispatch attempt).
resource "google_bigquery_dataset_iam_member" "tb_routing_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}

# ADR 0033 PR-C: the risk_flags polling SQL LEFT JOINs
# `airtable_replica.accounts` to enrich the Gmail-draft subject with
# the company name. Same posture as `tb_agent_triage_replica_viewer`
# (`triage_agent_iam.tf`) — the replica is HIPAA-filtered upstream by
# the Airtable sync's filterByFormula, so dataViewer here can't expose
# HIPAA-flagged account data.
resource "google_bigquery_dataset_iam_member" "tb_routing_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}

# ADR 0032: Gmail draft fan-out impersonates `asb-agent-triage-sa` to mint
# a `gmail.compose`-scoped credential at runtime (DWD subject =
# owner@example.com). SA-resource-scoped binding, NOT
# project-wide — keeps blast radius narrow.
#
# ADR 0027 §2 invariant preserved: `asb-agent-triage-sa` remains the only
# DWD-grantable SA. This binding governs *who can act as* that SA, which
# is orthogonal to *who is DWD-grantable*. Workspace admin still grants
# DWD on `asb-agent-triage-sa` only; routing's runtime SA is not added to
# the Workspace allowlist.
resource "google_service_account_iam_member" "tb_routing_can_impersonate_triage_for_dwd" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}
