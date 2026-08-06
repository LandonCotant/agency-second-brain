# WS-D Chat fan-out — Cloud Run Job + Cloud Scheduler (ADR 0023).
# Mirrors triage_bridge.tf one-for-one (ADR 0019).
#
# Resources:
#  - Cloud Scheduler invoker SA (per-job, holds run.invoker only)
#  - Cloud Run Job `asb-routing-fanout` (runs as `asb-routing-sa` from
#    routing_fanout_iam.tf — narrow IAM, no Pub/Sub or AI Platform)
#  - Cloud Scheduler `asb-routing-fanout-5m`
#
# Reuses the `asb-agents` Artifact Registry repo from triage_bridge.tf.
# Image lifecycle mirrors the triage bridge: TF declares the resource at
# the `bootstrap` tag, cloudbuild rebuilds the image on every PR for
# verification, and operator-driven `gcloud run jobs update` swaps tags
# post-merge. `lifecycle.ignore_changes = [image]` prevents TF from
# fighting that.

variable "routing_fanout_image_tag" {
  description = "Container tag for asb-routing-fanout. 'bootstrap' is a placeholder image so the first terraform apply succeeds before cloudbuild has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "routing_fanout_schedule" {
  description = "Cloud Scheduler cron expression. Default every 5 minutes — same cadence as the triage bridge so a critical message is dispatched within ~10 min of arrival end-to-end."
  type        = string
  default     = "*/5 * * * *"
}

variable "routing_fanout_lookback_minutes" {
  description = "How far back the fan-out worker scans triaged_items each tick. 30 min covers two scheduler ticks plus headroom for retries; rows older than this with no matching routed_events row are considered abandoned and logged but not dispatched."
  type        = number
  default     = 30
}

variable "routing_gmail_draft_recipient" {
  description = "Recipient mailbox for Gmail drafts created by the routing fan-out (ADR 0032). DWD subject; drafts land in this user's Drafts folder. Empty string disables the Gmail channel."
  type        = string
  default     = "owner@example.com"
}

# ---------------------------------------------------------------------------
# Service account — Cloud Scheduler invoker
# ---------------------------------------------------------------------------
# The Cloud Run Job runs as asb-routing-sa (declared in routing_fanout_iam.tf);
# only the OIDC invoker needs a fresh SA.

resource "google_service_account" "tb_routing_fanout_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-routing-fanout-invoker"
  display_name = "Cloud Scheduler invoker for asb-routing-fanout"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  routing_fanout_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/routing-fanout:${var.routing_fanout_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_routing_fanout" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-routing-fanout"

  template {
    template {
      service_account = google_service_account.tb_routing_sa.email
      timeout         = "300s" # 5-min cap fits inside the scheduler tick
      max_retries     = 0      # transient errors logged + retried next tick

      containers {
        image = local.routing_fanout_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "ROUTING_SA_EMAIL"
          value = google_service_account.tb_routing_sa.email
        }
        env {
          name  = "LOOKBACK_MINUTES"
          value = tostring(var.routing_fanout_lookback_minutes)
        }
        # ADR 0041: decisions WHERE status='draft' poll lookback. 24h
        # default matches risk_flags. The code default
        # (DEFAULT_DECISIONS_LOOKBACK_MINUTES in fanout_main.py)
        # already covers this, but setting the env var explicitly
        # makes the value visible in the Cloud Run console.
        env {
          name  = "DECISIONS_LOOKBACK_MINUTES"
          value = "1440"
        }
        env {
          name  = "CHAT_WEBHOOK_SECRET_ID"
          value = var.brain_alerts_chat_webhook_secret_id
        }
        env {
          # Latest version — the Cloud Run Job entrypoint resolves this
          # to a concrete URL via Secret Manager API at startup.
          name  = "CHAT_WEBHOOK_SECRET_VERSION"
          value = "latest"
        }
        env {
          name  = "DRY_RUN"
          value = "false"
        }

        # ADR 0032: Gmail draft channel via DWD impersonation of
        # `asb-agent-triage-sa`. The serviceAccountTokenCreator binding
        # (routing_fanout_iam.tf) lets `asb-routing-sa` mint a
        # gmail.compose-scoped credential at runtime.
        env {
          name  = "TRIAGE_SA_EMAIL"
          value = google_service_account.tb_agent_triage_sa.email
        }
        env {
          name  = "GMAIL_DRAFT_RECIPIENT"
          value = var.routing_gmail_draft_recipient
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
    google_project_iam_member.tb_routing_fanout_role,
    google_bigquery_dataset_iam_member.tb_routing_outputs_editor,
    google_bigquery_dataset_iam_member.tb_routing_audit_writer,
    google_service_account_iam_member.tb_routing_can_impersonate_triage_for_dwd,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "routing_fanout_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_routing_fanout.project
  location = google_cloud_run_v2_job.tb_routing_fanout.location
  name     = google_cloud_run_v2_job.tb_routing_fanout.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_routing_fanout_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — kicks off the job every 5 min
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_routing_fanout_5m" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-routing-fanout-5m"
  schedule  = var.routing_fanout_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the WS-D Chat fan-out worker. ADR 0023."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_routing_fanout.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_routing_fanout_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.routing_fanout_scheduler_invoker]
}
