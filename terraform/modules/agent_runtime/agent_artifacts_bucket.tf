# GCS bucket for Vertex AI Agent Engine staging artifacts.
#
# The Python SDK (vertexai.agent_engines.create) packages local source +
# requirements + agent state and uploads to a staging bucket before calling
# the Reasoning Engine create API. This bucket is the staging target.
#
# Pattern: one bucket per project, agents share it via gcs_dir_name = "<agent>".
# Triage uploads to gs://asb-agent-artifacts-prod/triage/.

resource "google_storage_bucket" "tb_agent_artifacts" {
  project       = var.brain_project_id
  name          = "${var.brain_project_id}-agent-artifacts"
  location      = "US"
  storage_class = "STANDARD"

  uniform_bucket_level_access = true # required by org policy
  public_access_prevention    = "enforced"

  versioning { enabled = true } # keep prior artifact versions for rollback

  lifecycle_rule {
    condition {
      age                = 90
      num_newer_versions = 5
    }
    action { type = "Delete" }
  }

  labels = {
    workstream = "agent_runtime"
    component  = "agent-artifacts"
  }
}

# The Triage Agent SA needs to read its own artifacts during RE bootstrap.
resource "google_storage_bucket_iam_member" "tb_agent_triage_artifacts_viewer" {
  bucket = google_storage_bucket.tb_agent_artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}
