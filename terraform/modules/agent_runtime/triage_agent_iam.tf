# WS-G1 Triage Agent — service account, custom IAM role, and dataset bindings.
# PRD §6.3 (agent specification) + §4.2 (least-privilege IAM, no predefined
# *.admin / editor / owner roles — `scripts/least_privilege_check.py` enforces).
#
# Patterns mirrored from:
# - terraform/modules/data_pipeline/airtable_sync.tf (sync SA + custom role)
# - terraform/modules/security/runtime_audits.tf  (audit SAs + dataset IAM)

resource "google_service_account" "tb_agent_triage_sa" {
  project      = var.brain_project_id
  account_id   = "asb-agent-triage-sa"
  display_name = "Agency Triage Agent"
  description  = "Runs the WS-G1 Triage Agent (PRD §6.3). Subscribes to asb-triage-input, classifies, writes agent_outputs.triaged_items + agent_audit_log.events. No Gmail send/modify per PRD §4.7 drafts boundary."
}

resource "google_project_iam_custom_role" "tb_agent_triage" {
  project     = var.brain_project_id
  role_id     = "tbAgentTriage"
  title       = "Agency Triage Agent"
  description = "Project-level permissions for the WS-G1 Triage Agent. BQ dataset access is granted separately via google_bigquery_dataset_iam_member."
  stage       = "GA"
  permissions = [
    # BigQuery query exec (data access scoped via dataset IAM below).
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Pub/Sub pull from asb-triage-input-sub.
    "pubsub.subscriptions.consume",
    "pubsub.subscriptions.get",
    # Vertex AI generate_content for classification (RE-internal call).
    "aiplatform.endpoints.predict",
    # Vertex AI Reasoning Engine — used by the Pub/Sub bridge to invoke
    # the deployed RE (PR 4d / ADR 0019). Both `.get` and `.query` are
    # required: `agent_engines.get(resource_name)` does a GET on the
    # engine to fetch its metadata, then `.query()` does the actual call.
    "aiplatform.reasoningEngines.get",
    "aiplatform.reasoningEngines.query",
    # Model Armor template enforcement at generate_content call time
    # (PRD §4.4 / ADR 0015 — Templates are referenced from the API call,
    # not configured on the RE resource).
    "modelarmor.templates.useToSanitizeUserPrompt",
    "modelarmor.templates.useToSanitizeModelResponse",
    # Agent Observability (Reasoning Engine traces).
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_agent_triage_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_agent_triage.id
  member  = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Model Armor "user" — predefined role for callers that USE Templates at
# generate_content time (PRD §4.4 / ADR 0015). Custom-role permissions for
# `modelarmor.templates.useToSanitize*` weren't sufficient on their own —
# Model Armor's IAM check apparently requires the binding via the
# predefined role rather than custom-role propagation.
resource "google_project_iam_member" "tb_agent_triage_modelarmor_user" {
  project = var.brain_project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Read goals + account_owners_v + tasks + service_catalog + risk_profiles for context.
# The replica is HIPAA-filtered upstream by the sync's filterByFormula, so this
# binding is safe — Triage cannot see HIPAA-flagged account data through it.
resource "google_bigquery_dataset_iam_member" "tb_agent_triage_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Write triaged_items rows (and read goals, risk_flags, etc. from agent_outputs).
resource "google_bigquery_dataset_iam_member" "tb_agent_triage_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Write audit events (mirrors how the audit jobs got dataEditor on this dataset).
resource "google_bigquery_dataset_iam_member" "tb_agent_triage_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Codify the manual `roles/iam.serviceAccountTokenCreator` grant on the
# Triage Agent SA to the operator (ADR 0027 §4). Lets the operator impersonate the SA
# via ADC for deploy/admin work — `scripts/deploy_triage_re.py`,
# `gcloud iam service-accounts get-access-token`, etc. The grant is on the
# SA resource itself, not project-wide, so blast radius is one SA.
resource "google_service_account_iam_member" "triage_token_creator_landon" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:owner@example.com"
}

# Self-impersonation: Morning Brief Cloud Run Job (asb-morning-brief) runs
# AS asb-agent-triage-sa (per ADR 0029 §1 — the only DWD-grantable SA per
# ADR 0027 §2). At runtime it builds DWD-scoped credentials via
# `impersonated_credentials.Credentials(target_principal=<self>)` so the
# `gmail.compose` and `calendar.readonly` scopes (ADR 0029 allowlist)
# can be minted. That self-impersonation requires signJwt on the SA's
# own resource — without this binding, calendar.events.list and
# gmail.drafts.create both 403 with `iam.serviceAccounts.signJwt denied`
# (observed in the 2026-05-03 first scheduled run).
#
# Scope: SA-resource-scoped, not project-wide. Same blast-radius posture
# as the routing-fan-out token-creator binding (ADR 0032 §4).
resource "google_service_account_iam_member" "triage_token_creator_self" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}
