# WS-G1 PR 4d — Pub/Sub → Reasoning Engine bridge.
# ADR 0019 (Cloud Run Job + Cloud Scheduler over Cloud Function / Eventarc).
#
# Resources:
#  - Artifact Registry repo `asb-agents` (new — kept separate from `asb-sync`
#    so future agent workers — Risk Watcher, Morning Brief — share a repo
#    that's named for what it holds)
#  - Cloud Scheduler invoker SA (per-job, holds run.invoker only)
#  - Cloud Run Job `asb-triage-bridge` (runs as the existing
#    `asb-agent-triage-sa` — that SA already has the Pub/Sub, RE, BQ, and
#    audit IAM; reusing it keeps audit attribution consistent across the
#    direct-RE-query path and the bridge path)
#  - Cloud Scheduler `asb-triage-bridge-5m`
#
# Image lifecycle mirrors `asb-airtable-sync`: TF declares the resource at
# the `bootstrap` tag, cloudbuild rebuilds the image on every PR for
# verification, and operator-driven `gcloud run jobs update` swaps tags
# post-merge. `lifecycle.ignore_changes = [image]` prevents TF from
# fighting that.

variable "triage_bridge_image_tag" {
  description = "Container tag for asb-triage-bridge. 'bootstrap' is a placeholder image so the first terraform apply succeeds before cloudbuild has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "triage_bridge_schedule" {
  description = "Cloud Scheduler cron expression. Default every 5 minutes — PRD §6.2 SLA is 10 min; 5-min cadence + sub-30s job time leaves comfortable retry headroom."
  type        = string
  default     = "*/5 * * * *"
}

variable "triage_inbox_project_record_id" {
  description = "Airtable Projects record id used as the 'Triage Inbox' default Project for Tasks the bridge drafts on a no-match (ADR 0019/0061). Same record id as TB_TRIAGE_INBOX_PROJECT_ID for the CRM Auto-updater. Empty disables the Airtable writer leg (classify-only)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service account — Cloud Scheduler invoker
# ---------------------------------------------------------------------------
# The Cloud Run Job itself runs as the existing asb-agent-triage-sa (declared
# in triage_agent_iam.tf); only the OIDC invoker needs a fresh SA.

resource "google_service_account" "tb_triage_bridge_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-triage-bridge-invoker"
  display_name = "Cloud Scheduler invoker for asb-triage-bridge"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Artifact Registry repository for agent worker images
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "tb_agents" {
  project       = var.brain_project_id
  location      = var.region
  repository_id = "asb-agents"
  format        = "DOCKER"
  description   = "Container images for WS-G agent workers (triage bridge, routing fan-out, future risk watcher, morning brief)."

  labels = {
    workstream = "ws-g"
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
  triage_bridge_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/triage-bridge:${var.triage_bridge_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_triage_bridge" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-triage-bridge"

  template {
    template {
      service_account = google_service_account.tb_agent_triage_sa.email
      timeout         = "600s" # matches subscription ack_deadline_seconds; must finish before next 5-min tick
      max_retries     = 0      # Pub/Sub already retries un-acked messages

      containers {
        image = local.triage_bridge_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        # ADR 0061: classification runs in-process here now (no Reasoning
        # Engine). The bridge owns the Airtable writer chain that the RE used
        # to own (ADR 0019), so it needs the same Airtable/inbox env the RE's
        # deploymentSpec carried. The SA (asb-agent-triage-sa) already holds the
        # secretAccessor binding on the PAT (triage_write_pat.tf).
        env {
          name  = "TRIAGE_SUB_PATH"
          value = "projects/${var.brain_project_id}/subscriptions/${google_pubsub_subscription.tb_triage_input_sub.name}"
        }
        env {
          name  = "AIRTABLE_OPS_BASE_ID"
          value = var.airtable_base_id
        }
        env {
          name  = "AIRTABLE_TASKS_WRITE_PAT_SECRET_ID"
          value = var.airtable_tasks_write_pat_secret_id
        }
        env {
          name  = "TB_TRIAGE_INBOX_PROJECT_ID"
          value = var.triage_inbox_project_record_id
        }
        env {
          name  = "TRIAGE_SA_EMAIL"
          value = google_service_account.tb_agent_triage_sa.email
        }
        env {
          name  = "MAX_MESSAGES"
          value = "50"
        }
        env {
          name  = "PULL_TIMEOUT_S"
          value = "30"
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
    google_pubsub_subscription_iam_member.triage_subscriber,
    google_bigquery_dataset_iam_member.tb_agent_triage_outputs_editor,
    google_bigquery_dataset_iam_member.tb_agent_triage_audit_writer,
    # ADR 0061: the bridge now reads the Airtable PAT directly (writer chain
    # folded in from the retired RE).
    google_secret_manager_secret_iam_member.triage_tasks_write_pat_accessor,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "triage_bridge_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_triage_bridge.project
  location = google_cloud_run_v2_job.tb_triage_bridge.location
  name     = google_cloud_run_v2_job.tb_triage_bridge.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_triage_bridge_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — kicks off the job every 5 min
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_triage_bridge_5m" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-triage-bridge-5m"
  schedule  = var.triage_bridge_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the Triage Pub/Sub → Reasoning Engine bridge. PRD §6.2, ADR 0019."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_triage_bridge.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_triage_bridge_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.triage_bridge_scheduler_invoker]
}
