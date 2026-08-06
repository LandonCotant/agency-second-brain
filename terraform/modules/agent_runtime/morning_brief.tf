# WS-G3 Morning Brief — Cloud Run Job + daily Cloud Scheduler.
# ADR 0029: topology mirrors triage_bridge (ADR 0019) but the trigger is a
# daily timer instead of a 5-min Pub/Sub poll. The Cloud Run Job runs as
# the existing asb-agent-triage-sa (one SA / one delegation surface, per
# ADR 0027 §2). DWD scopes: gmail.compose + calendar.readonly, granted at
# the Workspace level on asb-agent-triage-sa.
#
# Image lifecycle mirrors triage_bridge: TF declares the resource at the
# `bootstrap` tag, manual rebuilds via cloudbuild.morning-brief.yaml swap
# tags post-merge. `lifecycle.ignore_changes = [image]` prevents TF from
# fighting that.

variable "morning_brief_image_tag" {
  description = "Container tag for asb-morning-brief. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.morning-brief.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "morning_brief_schedule" {
  description = "Cloud Scheduler cron expression for the daily brief. Default 25 7 * * * (7:25am every day) interpreted in morning_brief_timezone."
  type        = string
  default     = "25 7 * * *"
}

variable "morning_brief_timezone" {
  description = "IANA tz for the morning_brief_schedule cron. Cloud Scheduler handles DST."
  type        = string
  default     = "America/Los_Angeles"
}

variable "morning_brief_recipients" {
  description = "Comma-separated list of Workspace user emails who receive the daily brief. v1: just the operator."
  type        = string
  default     = "owner@example.com"
}

# ---------------------------------------------------------------------------
# Service account — Cloud Scheduler invoker
# ---------------------------------------------------------------------------
# The Cloud Run Job itself runs as the existing asb-agent-triage-sa; only
# the OIDC invoker needs a fresh SA.

resource "google_service_account" "tb_morning_brief_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-morning-brief-invoker"
  display_name = "Cloud Scheduler invoker for asb-morning-brief"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  morning_brief_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/morning-brief:${var.morning_brief_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_morning_brief" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-morning-brief"

  # Cloud Run Jobs v2 defaults this to true, which blocks destroy+create
  # when the resource is tainted (e.g., after a failed first-deploy with
  # a missing image). The Job has no persistent state — execution
  # history lives in agent_audit_log.events; recreating is cheap.
  # Mirrors the same flag on asb-notes-ingestor (ADR 0031 deploy fix).
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_agent_triage_sa.email
      timeout         = "300s"
      max_retries     = 0

      containers {
        image = local.morning_brief_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRIEF_RECIPIENTS"
          value = var.morning_brief_recipients
        }
        env {
          name  = "BRIEF_TIMEZONE"
          value = var.morning_brief_timezone
        }
        env {
          name  = "TRIAGE_SA_EMAIL"
          value = google_service_account.tb_agent_triage_sa.email
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
    google_project_iam_member.tb_agent_triage_role,
    google_bigquery_dataset_iam_member.tb_agent_triage_outputs_editor,
    google_bigquery_dataset_iam_member.tb_agent_triage_replica_viewer,
    google_bigquery_dataset_iam_member.tb_agent_triage_audit_writer,
    google_bigquery_table.morning_briefs,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "morning_brief_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_morning_brief.project
  location = google_cloud_run_v2_job.tb_morning_brief.location
  name     = google_cloud_run_v2_job.tb_morning_brief.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_morning_brief_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily morning trigger
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_morning_brief_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-morning-brief-daily"
  schedule  = var.morning_brief_schedule
  time_zone = var.morning_brief_timezone

  description = "Daily kick-off for the Morning Brief Cloud Run Job. ADR 0029. Retired 2026-05-18 (ADR 0056) — Local Claude Code routine 'Morning brief daily' is the canonical surface; Gmail-draft output no longer read. Job + SA + IAM stay deployed for one-line revert."

  paused = true

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_morning_brief.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_morning_brief_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.morning_brief_scheduler_invoker]
}
