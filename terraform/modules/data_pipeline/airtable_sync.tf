# Airtable → BigQuery sync infrastructure (PR-2):
#  - Service account + custom IAM role (PRD §4.2 least-privilege)
#  - Resource-scoped bindings: Secret Manager, Pub/Sub, BigQuery dataset
#  - Artifact Registry repo for the sync image
#  - Cloud Run Job (executed by Cloud Scheduler every 15 min)
#  - Cloud Scheduler invoker SA with run.invoker on the job
#
# The PAT secret itself is provisioned manually before first apply (see
# docs/runbooks/airtable_pat_rotation.md) — Terraform references it by name
# only so the secret value never sits in tfvars.

variable "airtable_pat_secret_id" {
  description = "Secret Manager short name (without project path) holding the Airtable PAT. Created manually per docs/runbooks/airtable_pat_rotation.md."
  type        = string
  default     = "airtable-pat-prod"
}

variable "airtable_base_id" {
  description = "Airtable base ID the sync targets (appXXXXXXXXXXXXXX). Single base, post-ADR-0020 collapse."
  type        = string
  default     = ""
}

variable "airtable_sync_image_tag" {
  description = "Container tag for asb-airtable-sync. 'bootstrap' is a placeholder image so the first terraform apply succeeds before cloudbuild has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "airtable_sync_schedule" {
  description = "Cloud Scheduler cron expression. Default hourly. Downstream agents (Risk Watcher 05:00, CRM Auto-updater 06:15, Morning Brief 07:25, Evening Reflection 16:00 + 21:00) all read the daily-fresh state; sub-hour freshness is over-provisioned and the original PRD §6.2 < 10-min SLA wasn't justified by real consumer needs. HIPAA cascade propagation goes from 15-min worst case to ~1-hour worst case (Airtable filterByFormula remains the primary defense per PRD §4.1 layer 2; BQ replica is the secondary mirror)."
  type        = string
  default     = "0 * * * *"
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_sync_airtable_sa" {
  project      = var.brain_project_id
  account_id   = "asb-sync-airtable-sa"
  display_name = "Airtable → BQ sync"
  description  = "Runs airtable_to_bq.py inside Cloud Run Job asb-airtable-sync. Custom role only — no predefined roles (PRD §4.2 / §4.8)."
}

resource "google_service_account" "tb_airtable_sync_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-airtable-sync-invoker"
  display_name = "Cloud Scheduler invoker for asb-airtable-sync"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role — project-scoped permissions for the sync workload
# ---------------------------------------------------------------------------
#
# Why this list: BigQuery load jobs require ``bigquery.jobs.create`` at the
# project level (load jobs aren't dataset-scoped). The dataset-level
# bigquery.dataEditor binding below provides table read/write. Secret
# Manager and Pub/Sub bindings are resource-scoped so the role itself stays
# narrow.

resource "google_project_iam_custom_role" "tb_sync_airtable" {
  project     = var.brain_project_id
  role_id     = "tbSyncAirtable"
  title       = "Agency sync — Airtable → BQ"
  description = "Project-scoped permissions for asb-sync-airtable-sa. PRD §4.2 / §4.8."

  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
  ]

  stage = "GA"
}

resource "google_project_iam_member" "tb_sync_airtable_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_sync_airtable.id
  member  = "serviceAccount:${google_service_account.tb_sync_airtable_sa.email}"
}

# Dataset-scoped editor — gives table.get/getData/update/updateData on the
# replica dataset only. Custom dataset-level roles in Terraform are awkward;
# bigquery.dataEditor at this scope is the standard pragmatic compromise
# and the marginal risk is bounded to this single dataset.
resource "google_bigquery_dataset_iam_member" "tb_sync_airtable_replica_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.airtable_replica.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_sync_airtable_sa.email}"
}

# ---------------------------------------------------------------------------
# Resource-scoped bindings: Secret Manager + Pub/Sub
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "tb_sync_airtable_pat_accessor" {
  project   = var.brain_project_id
  secret_id = var.airtable_pat_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_sync_airtable_sa.email}"
}

resource "google_pubsub_topic_iam_member" "tb_sync_airtable_drift_publisher" {
  project = var.brain_project_id
  topic   = google_pubsub_topic.tb_schema_drift_alerts.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.tb_sync_airtable_sa.email}"
}

# ---------------------------------------------------------------------------
# Artifact Registry repository for the sync image
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "tb_sync" {
  project       = var.brain_project_id
  location      = var.region
  repository_id = "asb-sync"
  format        = "DOCKER"
  description   = "Container images for sync workloads (airtable_to_bq, future vantage_federation)."

  labels = {
    workstream = "ws-b"
  }

  # ADR 0024: keep the 5 most recent versions per tag and delete anything
  # older than 90 days. Both rules apply together; the live `:bootstrap`
  # tag is always one of the 5 most recent so it's never deleted.
  cleanup_policies {
    id     = "keep-recent-5"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "7776000s" # 90 days
    }
  }
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  airtable_sync_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_sync.repository_id}/airtable-to-bq:${var.airtable_sync_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_airtable_sync" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-airtable-sync"

  template {
    template {
      service_account = google_service_account.tb_sync_airtable_sa.email
      timeout         = "900s" # 15 min — must finish before the next scheduler tick

      containers {
        image = local.airtable_sync_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "AIRTABLE_BASE_ID"
          value = var.airtable_base_id
        }
        env {
          name  = "AIRTABLE_PAT_SECRET_ID"
          value = var.airtable_pat_secret_id
        }
        env {
          name  = "SCHEMA_DRIFT_TOPIC"
          value = google_pubsub_topic.tb_schema_drift_alerts.name
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
    google_project_iam_member.tb_sync_airtable_role,
    google_bigquery_dataset_iam_member.tb_sync_airtable_replica_editor,
    google_secret_manager_secret_iam_member.tb_sync_airtable_pat_accessor,
    google_pubsub_topic_iam_member.tb_sync_airtable_drift_publisher,
  ]

  # The image tag is overwritten by the cloudbuild push step on every main
  # merge — Terraform should not fight that change.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_airtable_sync.project
  location = google_cloud_run_v2_job.tb_airtable_sync.location
  name     = google_cloud_run_v2_job.tb_airtable_sync.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_airtable_sync_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — kicks off the job on the schedule from
# var.airtable_sync_schedule (hourly by default).
# ---------------------------------------------------------------------------

# Resource name + deployed name carry "_15m" / "-15m" suffix for
# historical reasons (original cadence was every 15 minutes). Renaming
# would force a destroy+create on the scheduler; keeping the name
# avoids that. The actual cadence is the variable above.
resource "google_cloud_scheduler_job" "tb_airtable_sync_15m" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-airtable-sync-15m"
  schedule  = var.airtable_sync_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the Airtable → BigQuery sync. PRD §6.2."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_airtable_sync.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_airtable_sync_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
