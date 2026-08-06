# ADR 0043 — Brag Spotter: Cloud Run Job + Sunday weekly Cloud Scheduler.
#
# Sweeps the last 7 days across triaged_items / routed_events / notes /
# decisions / evening_reflections for win signals; writes new wins into
# agent_outputs.wins (per-week dedup key); dispatches a Sunday-evening
# Chat card + Gmail draft.
#
# Topology mirrors morning-brief (ADR 0029): Cloud Run Job + Cloud
# Scheduler + dedicated invoker SA. The Job runs as a NEW SA
# `asb-brag-spotter-sa` (not `asb-agent-triage-sa`) — Brag Spotter doesn't
# need DWD; Gmail drafts are sent via impersonating asb-agent-triage-sa
# (the SA-resource-scoped binding below mirrors the routing-fanout
# pattern from ADR 0032 §4). Per ADR 0027 §2 invariant, only
# asb-agent-triage-sa is DWD-grantable; asb-brag-spotter-sa impersonates it.
#
# Image lifecycle mirrors notes-ingestor / captures-materializer: TF
# declares the resource at the `bootstrap` tag; manual rebuilds via
# cloudbuild.brag-spotter.yaml swap tags post-merge.
# `lifecycle.ignore_changes = [image]` prevents TF from fighting that.

variable "brag_spotter_image_tag" {
  description = "Container tag for asb-brag-spotter (ADR 0043). 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.brag-spotter.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "brag_spotter_schedule" {
  description = "Cloud Scheduler cron for the weekly digest. Default '0 18 * * 0' = Sunday 18:00 in brag_spotter_timezone."
  type        = string
  default     = "0 18 * * 0"
}

variable "brag_spotter_timezone" {
  description = "IANA tz for brag_spotter_schedule. Sunday-evening cadence runs after the day's reflection."
  type        = string
  default     = "America/Los_Angeles"
}

variable "brag_spotter_recipients" {
  description = "Comma-separated recipient list. v1: the operator."
  type        = string
  default     = "owner@example.com"
}

variable "brag_spotter_lookback_days" {
  description = "Window for source readers (ADR 0043 §3). Default 7 — the week boundary."
  type        = string
  default     = "7"
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_brag_spotter_sa" {
  project      = var.brain_project_id
  account_id   = "asb-brag-spotter-sa"
  display_name = "Agency Brag Spotter"
  description  = "Runs the WS-G PKM Phase 3 Brag Spotter weekly Job (ADR 0043). Reads 5 agent_outputs sources; writes wins + audit; impersonates asb-agent-triage-sa for gmail.compose drafts."
}

resource "google_service_account" "tb_brag_spotter_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-brag-spotter-invoker"
  display_name = "Cloud Scheduler invoker for asb-brag-spotter"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + project bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_brag_spotter" {
  project     = var.brain_project_id
  role_id     = "tbBragSpotter"
  title       = "Agency Brag Spotter"
  description = "Project-level permissions for the WS-G PKM Phase 3 Brag Spotter. BQ dataset access granted separately via google_bigquery_dataset_iam_member. ADR 0043."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Vertex Gemini structured response (gemini-2.5-flash + response_schema)
    "aiplatform.endpoints.predict",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_brag_spotter_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_brag_spotter.id
  member  = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

# ---------------------------------------------------------------------------
# Dataset bindings
# ---------------------------------------------------------------------------

# Read agent_outputs.* (5 source tables + existing wins for dedup pre-check).
# Write agent_outputs.wins + audit log (BaseAgent contract).
resource "google_bigquery_dataset_iam_member" "tb_brag_spotter_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_brag_spotter_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

# airtable_replica is read-only here — used if any reader needs to JOIN
# back to Account/Contact/Project context (v1 doesn't, but the ADR 0040
# precedent reads replica too; granting up-front avoids a future PR).
resource "google_bigquery_dataset_iam_member" "tb_brag_spotter_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

# ---------------------------------------------------------------------------
# DWD impersonation — asb-brag-spotter-sa → asb-agent-triage-sa for gmail.compose
# ---------------------------------------------------------------------------
# Mirrors the routing-fanout pattern (ADR 0032 §4) and the risk-watcher
# calendar-readonly pattern (ADR 0034 §4). The binding is SA-resource-scoped:
# asb-brag-spotter-sa can mint downscoped credentials for asb-agent-triage-sa
# but cannot impersonate any other SA. ADR 0027 §2 invariant preserved
# (asb-agent-triage-sa is the only DWD-grantable SA; everyone else
# impersonates).

resource "google_service_account_iam_member" "brag_spotter_impersonates_triage" {
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

# ---------------------------------------------------------------------------
# Chat webhook secret accessor
# ---------------------------------------------------------------------------
# Sunday digest Chat card uses the same `second-brain-gchat-webhook` secret
# the routing-fanout uses (ADR 0023 / 0033). Per-SA accessor binding —
# does not touch existing routing-fanout binding.

resource "google_secret_manager_secret_iam_member" "tb_brag_spotter_chat_secret_accessor" {
  project   = var.brain_project_id
  secret_id = "second-brain-gchat-webhook"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_brag_spotter_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  brag_spotter_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/brag-spotter:${var.brag_spotter_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_brag_spotter" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-brag-spotter"

  # Same posture as morning-brief / notes-ingestor / captures-materializer:
  # deletion_protection off so a tainted resource (e.g. failed first-deploy
  # with a missing image) can be recreated. Job has no persistent state —
  # execution history lives in agent_audit_log.events.
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_brag_spotter_sa.email
      timeout         = "300s"
      max_retries     = 0

      containers {
        image = local.brag_spotter_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRAG_SPOTTER_RECIPIENTS"
          value = var.brag_spotter_recipients
        }
        env {
          name  = "BRAG_SPOTTER_TIMEZONE"
          value = var.brag_spotter_timezone
        }
        env {
          name  = "TRIAGE_SA_EMAIL"
          value = google_service_account.tb_agent_triage_sa.email
        }
        env {
          name  = "LOOKBACK_DAYS"
          value = var.brag_spotter_lookback_days
        }
        env {
          name  = "CHAT_WEBHOOK_SECRET_ID"
          value = "second-brain-gchat-webhook"
        }
        env {
          name  = "CHAT_WEBHOOK_SECRET_VERSION"
          value = "latest"
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
    google_project_iam_member.tb_brag_spotter_role,
    google_bigquery_dataset_iam_member.tb_brag_spotter_outputs_editor,
    google_bigquery_dataset_iam_member.tb_brag_spotter_audit_writer,
    google_bigquery_dataset_iam_member.tb_brag_spotter_replica_viewer,
    google_service_account_iam_member.brag_spotter_impersonates_triage,
    google_secret_manager_secret_iam_member.tb_brag_spotter_chat_secret_accessor,
    google_bigquery_table.wins,
    google_bigquery_table.decisions,
    google_bigquery_table.notes,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "brag_spotter_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_brag_spotter.project
  location = google_cloud_run_v2_job.tb_brag_spotter.location
  name     = google_cloud_run_v2_job.tb_brag_spotter.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_brag_spotter_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — Sunday 18:00 PT weekly trigger
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_brag_spotter_weekly" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-brag-spotter-weekly"
  schedule  = var.brag_spotter_schedule
  time_zone = var.brag_spotter_timezone

  description = "Sunday-evening kick-off for the Brag Spotter Cloud Run Job. ADR 0043."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_brag_spotter.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_brag_spotter_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.brag_spotter_scheduler_invoker]
}
