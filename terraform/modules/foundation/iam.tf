# PRD §4.2: no shared service accounts. WS-A creates none — each workstream
# provisions its own SA in its own module. WS-A only sets human IAM and the
# Cloud Build SA permissions needed for CI.

# Owner is granted directly to the human admin (var.owner_email) for now.
# When a asb-admins@ group is provisioned in Workspace, replace these with
# group bindings via PR. PRD §4.2 explicitly allows humans (user/group) to
# hold roles/owner during bootstrap; only service accounts may not.
resource "google_project_iam_member" "brain_admin_owner" {
  project = google_project.brain.project_id
  role    = "roles/owner"
  member  = "user:${var.owner_email}"
}

# Optional group owner binding — only created if admin_group_email is non-empty.
# Lets us defer Workspace group creation without blocking apply.
resource "google_project_iam_member" "brain_admin_group_owner" {
  count = var.admin_group_email == "" ? 0 : 1

  project = google_project.brain.project_id
  role    = "roles/owner"
  member  = "group:${var.admin_group_email}"
}

# Custom Cloud Build SA for PR checks.
#
# Google disabled the legacy <project-number>@cloudbuild.gserviceaccount.com
# default for projects created after April 2024, so we provision our own and
# grant the minimum the pipeline needs: read source/TF state, run
# `terraform plan` against live state, write build logs. No apply role — apply
# still happens locally after PR merge (per PRD §4.2 / WS-A scope).
#
# Matches the per-workstream SA pattern used elsewhere in the project
# (asb-sync-airtable-sa, asb-runtime-audit-inv, asb-audit-*).

resource "google_service_account" "cloud_build" {
  project      = google_project.brain.project_id
  account_id   = "asb-cloud-build-sa"
  display_name = "Cloud Build PR-checks SA"
  description  = "Runs cloudbuild.yaml on every PR. Permissions in foundation/iam.tf."

  depends_on = [google_project_service.brain]
}

locals {
  cloud_build_sa = "serviceAccount:${google_service_account.cloud_build.email}"
}

# Cloud Build needs to read TF state to run `terraform plan` in PR checks.
resource "google_storage_bucket_iam_member" "cb_tfstate_read" {
  bucket = "asb-tfstate-prod"
  role   = "roles/storage.objectViewer"
  member = local.cloud_build_sa

  # The state bucket is created by scripts/bootstrap_tfstate.sh before first apply.
  # Once apply runs, this binding becomes managed in TF state.
}

# Cloud Build needs viewer on the project to evaluate `terraform plan` against
# live resource state. No write/admin roles — apply happens locally by the operator
# after PR merge until WS-E adds a CD pipeline.
#
# ADR 0065: roles/viewer (read-only metadata, NOT secret values) is
# accepted rather than narrowed to a custom role. terraform plan reads the
# full resource graph; a hand-built read role would break CI on every new
# resource type for marginal risk reduction on an already-supply-chain-trust
# build SA. The IAM drift audit baselines this binding, so any escalation
# (e.g. adding editor) trips SECURITY_DRIFT.
resource "google_project_iam_member" "cb_viewer" {
  project = google_project.brain.project_id
  role    = "roles/viewer"
  member  = local.cloud_build_sa
}

# Cloud Build needs to write build logs. The legacy default SA got this
# implicitly via Cloud Build's own setup; a custom SA must be granted it
# explicitly, otherwise builds fail with permission errors on log streaming
# (cloudbuild.yaml uses logging: CLOUD_LOGGING_ONLY).
resource "google_project_iam_member" "cb_log_writer" {
  project = google_project.brain.project_id
  role    = "roles/logging.logWriter"
  member  = local.cloud_build_sa
}

# Cloud Build needs to push images to Artifact Registry. The legacy default
# Cloud Build SA had this implicitly; once ADR 0018 disabled the legacy SA
# and asb-cloud-build-sa took over, manual `gcloud builds submit` invocations
# (per docs/runbooks/runtime_audit_response.md) started failing with
# `artifactregistry.repositories.uploadArtifacts denied`. PR-time builds use
# build-only steps so they didn't catch the gap; surfaced when re-pushing
# the audit image post-PR-54. Project-scope grant is appropriate here:
# asb-cloud-build-sa is THE PR-build SA, by design touches every CI image.
resource "google_project_iam_member" "cb_artifact_writer" {
  project = google_project.brain.project_id
  role    = "roles/artifactregistry.writer"
  member  = local.cloud_build_sa
}

# ---------------------------------------------------------------------------
# Compute Engine default SA — held in DISABLED state.
# ---------------------------------------------------------------------------
# Enabling cloudbuild.googleapis.com transitively enables
# containerregistry.googleapis.com → compute.googleapis.com, which auto-creates
# <project_number>-compute@developer.gserviceaccount.com. PRD §4.2 forbids
# shared / unowned service accounts, so we manage it here.
#
# The Editor auto-grant for default SAs is already prevented at the org level
# by iam.automaticIamGrantsForDefaultServiceAccounts (verified: empty IAM
# bindings for this SA on the project). Disabling the SA is the load-bearing
# control: even if a future operator adds a binding, the SA cannot mint tokens
# while disabled.
#
# We do not delete it — soft-delete is recoverable for 30 days, and any future
# re-enable of compute.googleapis.com would resurrect the SA. Disabled is the
# stable, declarative state.
#
# We cannot manage this with `google_service_account` because the account_id
# would be "<project_number>-compute", which fails GCP's account_id regex
# `^[a-z]([-a-z0-9]*[a-z0-9])?$` (it starts with a digit). The purpose-built
# resource for this is `google_project_default_service_accounts`, which acts
# on default SAs by service without requiring us to declare the SA itself.
# See ADR 0018 for the full reasoning.
resource "google_project_default_service_accounts" "deprivilege_defaults" {
  project = google_project.brain.project_id
  action  = "DISABLE"

  depends_on = [google_project_service.brain]
}
