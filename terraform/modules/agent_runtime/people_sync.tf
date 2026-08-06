# ADR 0057 — People Sync: Cloud Run Job + weekly Sunday scheduler.
#
# Reads airtable_replica.{accounts,contacts}; materializes one .md per
# row under Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/ with YAML frontmatter
# (warmth, relationship_type, last_contact, next_followup, etc.).
# Librarian's Galaxy sweep indexes them → wikilinks resolve into
# notes_links graph edges (ADR 0053). HIPAA cascade excluded.
#
# Topology mirrors captures_materializer (ADR 0039): Cloud Run Job +
# scheduler + dedicated runtime SA + dedicated invoker SA. Image lifecycle
# uses lifecycle.ignore_changes = [image] so cloudbuild can swap tags
# post-merge without TF re-asserting the bootstrap tag.

variable "people_sync_image_tag" {
  description = "Container tag for asb-people-sync (ADR 0057). 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.people-sync.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "people_sync_schedule" {
  description = "Cloud Scheduler cron for the people sync. Default '15 6 * * 0' = Sunday 06:15 UTC, aligned with the Sunday 8 AM PT weekly-review-reflect routine."
  type        = string
  default     = "15 6 * * 0"
}

variable "brain_galaxy_accounts_folder_id" {
  description = "ADR 0057 — Drive folder id of Brain/05_GALAXY/01_ACCOUNTS/. People Sync writes one .md per active Account here. Empty disables the accounts side of the sync."
  type        = string
  default     = ""
}

variable "brain_galaxy_contacts_folder_id" {
  description = "ADR 0057 — Drive folder id of Brain/05_GALAXY/02_CONTACTS/. People Sync writes one .md per non-HIPAA Contact here. Empty disables the contacts side of the sync."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_people_sync_sa" {
  project      = var.brain_project_id
  account_id   = "asb-people-sync-sa"
  display_name = "Agency People Sync"
  description  = "Runs the People Sync Cloud Run Job (ADR 0057). Reads airtable_replica.{accounts,contacts}; writes markdown files to Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/ via folder-share + ADC (ADR 0044). No DWD."
}

resource "google_service_account" "tb_people_sync_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-people-sync-invoker"
  display_name = "Cloud Scheduler invoker for asb-people-sync"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_people_sync" {
  project     = var.brain_project_id
  role_id     = "tbPeopleSync"
  title       = "Agency People Sync"
  description = "Project-level permissions for asb-people-sync (ADR 0057). BQ dataset access granted separately via google_bigquery_dataset_iam_member."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_people_sync_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_people_sync.id
  member  = "serviceAccount:${google_service_account.tb_people_sync_sa.email}"
}

# Read airtable_replica.{accounts,contacts,projects,risk_flags} +
# agent_outputs.{triaged_items,notes} for the enricher.
resource "google_bigquery_dataset_iam_member" "tb_people_sync_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_people_sync_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_people_sync_outputs_viewer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_people_sync_sa.email}"
}

# Audit row writer.
resource "google_bigquery_dataset_iam_member" "tb_people_sync_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_people_sync_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  people_sync_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/people-sync:${var.people_sync_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_people_sync" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-people-sync"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_people_sync_sa.email
      timeout         = "600s"
      max_retries     = 0

      containers {
        image = local.people_sync_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRAIN_GALAXY_ACCOUNTS_FOLDER_ID"
          value = var.brain_galaxy_accounts_folder_id
        }
        env {
          name  = "BRAIN_GALAXY_CONTACTS_FOLDER_ID"
          value = var.brain_galaxy_contacts_folder_id
        }
        env {
          name  = "PEOPLE_SYNC_SA_EMAIL"
          value = google_service_account.tb_people_sync_sa.email
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.tb_people_sync_role,
    google_bigquery_dataset_iam_member.tb_people_sync_replica_viewer,
    google_bigquery_dataset_iam_member.tb_people_sync_outputs_viewer,
    google_bigquery_dataset_iam_member.tb_people_sync_audit_writer,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "people_sync_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_people_sync.project
  location = google_cloud_run_v2_job.tb_people_sync.location
  name     = google_cloud_run_v2_job.tb_people_sync.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_people_sync_invoker_sa.email}"
}

# Note: the `sync_people` MCP tool calls `gcloud run jobs execute` under
# owner@example.com's local ADC. owner@ holds Owner on the
# project (which includes roles/run.invoker), so no explicit user IAM
# binding is needed here. If a non-owner user ever wants to fire the
# job, add a binding for that principal.

# ---------------------------------------------------------------------------
# Cloud Scheduler — weekly Sunday 06:15 UTC
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_people_sync_weekly" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-people-sync-weekly"
  schedule  = var.people_sync_schedule
  time_zone = "Etc/UTC"

  description = "Weekly Sunday 06:15 UTC sync of airtable_replica.{accounts,contacts} into Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/ markdown files. ADR 0057. Manual on-demand runs via the sync_people MCP tool or `gcloud run jobs execute`."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "120s"
    min_backoff_duration = "10s"
    max_backoff_duration = "60s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_people_sync.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_people_sync_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.people_sync_scheduler_invoker]
}
