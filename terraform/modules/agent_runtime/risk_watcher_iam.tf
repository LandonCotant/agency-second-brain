# WS-G2 Risk Watcher — service account, custom IAM role, and dataset
# bindings (ADR 0033, PR-A skeleton).
#
# This file ONLY ships the SA + custom role + BQ dataset bindings.
# The Cloud Run Job + scheduler land in PR-B's `risk_watcher.tf`
# alongside the first deployable image. Per ADR 0033 §5, deploying
# an empty Cloud Run Job in PR-A is intentionally rejected — we don't
# want a scheduled empty tick before any signals have been written.
#
# Patterns mirrored from:
# - terraform/modules/agent_runtime/notes_ingestor.tf (newest agent
#   topology — PR #65, ADR 0031)
# - terraform/modules/agent_runtime/triage_agent_iam.tf (custom-role
#   shape used across the WS-G family)

resource "google_service_account" "tb_risk_watcher_sa" {
  project      = var.brain_project_id
  account_id   = "asb-risk-watcher-sa"
  display_name = "Agency Risk Watcher"
  description  = "Runs the WS-G2 Risk Watcher (ADR 0033 + 0034). Reads airtable_replica; writes agent_outputs.risk_flags + agent_audit_log.events. Indirect DWD: impersonates asb-agent-triage-sa for calendar.readonly (ADR 0034 §4 Owner Disengagement). No direct DWD grant."
}

resource "google_service_account" "tb_risk_watcher_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-risk-watcher-invoker"
  display_name = "Cloud Scheduler invoker for asb-risk-watcher"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC. Bound to the Cloud Run Job in PR-B's risk_watcher.tf."
}

# ---------------------------------------------------------------------------
# Custom IAM role
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_risk_watcher" {
  project     = var.brain_project_id
  role_id     = "tbRiskWatcher"
  title       = "Agency Risk Watcher"
  description = "Project-level permissions for the WS-G2 Risk Watcher. BQ dataset access is granted separately via google_bigquery_dataset_iam_member. ADR 0033."
  stage       = "GA"
  permissions = [
    # BigQuery query exec (data access scoped via dataset IAM below).
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Vertex AI generate_content for Gemini reasoning (PR-B's signals
    # may call the model directly; ADR 0033 §1 forbids Reasoning
    # Engine deploy — Vertex SDK direct only, mirrors ADR 0029 §3).
    "aiplatform.endpoints.predict",
    # Agent Observability traces (PRD §8.1 native dashboards).
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_risk_watcher_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_risk_watcher.id
  member  = "serviceAccount:${google_service_account.tb_risk_watcher_sa.email}"
}

# ---------------------------------------------------------------------------
# BigQuery dataset bindings
# ---------------------------------------------------------------------------

# Read accounts/contacts/projects/risk_profiles/tasks for client state
# assembly. The replica is HIPAA-filtered upstream by the airtable
# sync's filterByFormula, so this binding can't see HIPAA-flagged
# account data. Same posture as `tb_agent_triage_replica_viewer`.
resource "google_bigquery_dataset_iam_member" "tb_risk_watcher_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_risk_watcher_sa.email}"
}

# Write risk_flags rows + read for same-day dedup pre-check.
resource "google_bigquery_dataset_iam_member" "tb_risk_watcher_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_risk_watcher_sa.email}"
}

# Write per-invocation audit rows (BaseAgent contract, ADR 0006).
resource "google_bigquery_dataset_iam_member" "tb_risk_watcher_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_risk_watcher_sa.email}"
}

# ADR 0034 §4: the Local Service Owner Disengagement signal reads the
# agency owner's calendar via DWD (`calendar.readonly`). The Risk
# Watcher SA mints a downscoped credential by impersonating
# `asb-agent-triage-sa` (the only DWD-grantable SA per ADR 0027 §2),
# then the impersonated identity carries the DWD scope with
# subject = owner@example.com.
#
# Mirrors the routing-fanout binding pattern from
# `routing_fanout_iam.tf` (ADR 0032 §4): SA-resource-scoped, NOT
# project-wide, so blast radius stays narrow. ADR 0027 §2 invariant
# preserved — `asb-risk-watcher-sa` is an indirect impersonator, not a
# DWD-grantable SA itself; Workspace admin's DWD allowlist still
# applies only to `asb-agent-triage-sa`'s client_id.
resource "google_service_account_iam_member" "tb_risk_watcher_can_impersonate_triage_for_dwd" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_risk_watcher_sa.email}"
}
