# ADR 0033 — Risk Watcher Cloud Run Job + daily scheduler (PR-B).
#
# Topology mirrors notes_ingestor.tf (ADR 0031) and morning_brief.tf
# (ADR 0029): Cloud Run Job + Cloud Scheduler + dedicated invoker SA
# (run.invoker only). The Job runs as `asb-risk-watcher-sa` declared in
# `risk_watcher_iam.tf` (PR-A). No DWD; no Pub/Sub; BQ read/write +
# Vertex predict only.
#
# Daily 06:00 Pacific so flags are visible to the 7:25am Morning Brief
# tick (ADR 0029) on the same day. ADR 0033 §1 rationale: signal
# windows are days/weeks, sub-daily cadence is wasted compute.

variable "risk_watcher_image_tag" {
  description = "Container tag for asb-risk-watcher. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.risk-watcher.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "risk_watcher_schedule" {
  description = "Cloud Scheduler cron for the risk watcher. Default '0 13 * * *' (13:00 UTC = 06:00 Pacific) per ADR 0033 §1 — visible to the 07:25 PT Morning Brief tick."
  type        = string
  default     = "0 13 * * *"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  risk_watcher_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/risk-watcher:${var.risk_watcher_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_risk_watcher" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-risk-watcher"

  # Same posture as notes_ingestor / morning_brief: no persistent
  # state; recreate is cheap. Default-true on Cloud Run Jobs v2 blocks
  # destroy+create when the resource is tainted (e.g., after a failed
  # first-deploy with a missing image).
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_risk_watcher_sa.email
      timeout         = "600s" # 10-min cap; daily cadence + small account count
      max_retries     = 0      # per-flag write errors land in audit rows; next tick retries via dedup

      containers {
        image = local.risk_watcher_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "RISK_WATCHER_SA_EMAIL"
          value = google_service_account.tb_risk_watcher_sa.email
        }
        env {
          name  = "MODEL_NAME"
          value = "deterministic" # PR-D signals are calendar math; future PRs may flip to gemini
        }
        env {
          name  = "PROMPT_VERSION"
          value = "v1"
        }
        # ADR 0034 §4: Owner Disengagement (Local Service) reads the
        # owner's calendar via DWD impersonation of asb-agent-triage-sa.
        env {
          name  = "RISK_WATCHER_DWD_TARGET_SA"
          value = google_service_account.tb_agent_triage_sa.email
        }
        env {
          name  = "RISK_WATCHER_OWNER_CALENDAR_SUBJECT"
          value = "owner@example.com"
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
    google_project_iam_member.tb_risk_watcher_role,
    google_bigquery_dataset_iam_member.tb_risk_watcher_replica_viewer,
    google_bigquery_dataset_iam_member.tb_risk_watcher_outputs_editor,
    google_bigquery_dataset_iam_member.tb_risk_watcher_audit_writer,
    google_service_account_iam_member.tb_risk_watcher_can_impersonate_triage_for_dwd,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "risk_watcher_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_risk_watcher.project
  location = google_cloud_run_v2_job.tb_risk_watcher.location
  name     = google_cloud_run_v2_job.tb_risk_watcher.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_risk_watcher_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily, paused initially
# ---------------------------------------------------------------------------
# `paused = true` on first-deploy: ADR 0033 §1 says one tick per day,
# but a tick before the image has been built (it ships at the
# `bootstrap` tag) would just exit-code-0 with no useful output. The
# unpause step is a manual `gcloud scheduler jobs resume` after the
# first real image lands, mirroring the WS-D Chat fan-out pattern
# (PR #46) where the scheduler was paused on TF apply and unpaused
# after the image swap.

resource "google_cloud_scheduler_job" "tb_risk_watcher_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-risk-watcher-daily"
  schedule  = var.risk_watcher_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the WS-G2 Risk Watcher daily (ADR 0033 §1). 06:00 Pacific so flags are visible to the 07:25 PT Morning Brief on the same day. Paused on first-deploy until the bootstrap image is replaced; unpause via `gcloud scheduler jobs resume asb-risk-watcher-daily`."

  paused = true

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_risk_watcher.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_risk_watcher_invoker_sa.email
    }
  }

  # Once unpaused via gcloud after smoke, ignore TF flipping it back.
  # Mirrors the evening_reflection.tf pattern. Without this guard, a
  # blanket `terraform apply` would re-pause the scheduler (the user
  # already unpaused it manually 2026-05-06 18:18 UTC). Surfaced
  # 2026-05-07 during ADR 0042 PR-C post-merge drift audit.
  lifecycle {
    ignore_changes = [paused]
  }

  depends_on = [google_cloud_run_v2_job_iam_member.risk_watcher_scheduler_invoker]
}
