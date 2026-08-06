# ADR 0046 — Calendar ingester (Workstream A, PR A2).
#
# Daily Cloud Run Job that pulls Google Calendar events into
# agent_outputs.notes for VECTOR_SEARCH retrieval by the Knowledge
# Surfacer. Topology mirrors notes_ingestor.tf (ADR 0031): Cloud Run
# Job + Cloud Scheduler + dedicated invoker SA with run.invoker only.
#
# DWD: REUSES the existing calendar.readonly scope on
# asb-agent-triage-sa (ADR 0027 §2 / ADR 0029) — NO new scope. The
# calendar ingester impersonates asb-agent-triage-sa via
# common/dwd.py::DWDServiceFactory exactly as the morning_brief +
# risk_watcher already do. ADR 0027 §3 invariant preserved.

variable "calendar_ingester_image_tag" {
  description = "Container tag for asb-calendar-ingester. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.calendar-ingester.yaml has built and pushed a real image."
  type        = string
  default     = "bootstrap"
}

variable "calendar_ingester_schedule" {
  description = "Cloud Scheduler cron for the calendar ingester. Default '30 13 * * *' (daily 13:30 UTC = 06:30 Pacific). Runs after notes_ingestor (06:00 PT) so daily voice-memo and calendar context land before the morning brief at 07:25 PT."
  type        = string
  default     = "30 13 * * *"
}

variable "calendar_ingester_calendars" {
  description = "Comma-separated calendar IDs to pull. Default 'primary'. Add other calendars (work calendar, shared agency calendar) by supplying the calendarId. Each is impersonated under the DWD subject."
  type        = string
  default     = "primary"
}

variable "calendar_ingester_lookback_days" {
  description = "How far back to pull events (days)."
  type        = string
  default     = "180"
}

variable "calendar_ingester_lookahead_days" {
  description = "How far forward to pull events (days). Future events also useful for 'when am I next meeting with X' queries."
  type        = string
  default     = "90"
}

variable "calendar_ingester_notes_scope" {
  description = "Scope value persisted on agent_outputs.notes.scope rows from the calendar ingester. ADR 0037 §1: 'agency' or 'personal'. Default 'agency' since calendar entries primarily reflect client meetings; revisit if a personal-calendar lane is added."
  type        = string
  default     = "agency"
}

variable "calendar_ingester_dwd_subject" {
  description = "DWD subject (mailbox owner) for calendar.readonly impersonation. Defaults to owner@example.com — the human user whose calendar is read."
  type        = string
  default     = "owner@example.com"
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_calendar_ingester_sa" {
  project      = var.brain_project_id
  account_id   = "asb-calendar-ingester-sa"
  display_name = "Agency Calendar Ingester"
  description  = "Runs the Calendar ingester (ADR 0046). Impersonates asb-agent-triage-sa for calendar.readonly. Writes agent_outputs.notes (calendar_event rows) + audit log. Reads airtable_replica.accounts for the HIPAA filter."
}

resource "google_service_account" "tb_calendar_ingester_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-calendar-ingester-invoker"
  display_name = "Cloud Scheduler invoker for asb-calendar-ingester"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_calendar_ingester" {
  project     = var.brain_project_id
  role_id     = "tbCalendarIngester"
  title       = "Agency Calendar Ingester"
  description = "Project-level permissions for the Calendar ingester. BQ dataset access granted separately via google_bigquery_dataset_iam_member. ADR 0046."
  stage       = "GA"
  permissions = [
    # BigQuery query exec (data access scoped via dataset IAM below).
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Vertex embedding for text-embedding-005.
    "aiplatform.endpoints.predict",
    # SA token creator on asb-agent-triage-sa is granted at the resource
    # level below — this custom role does not include serviceAccounts.*
    # so the impersonation surface is bounded at the resource binding.
    # Agent Observability traces.
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_calendar_ingester_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_calendar_ingester.id
  member  = "serviceAccount:${google_service_account.tb_calendar_ingester_sa.email}"
}

# ServiceAccountTokenCreator on asb-agent-triage-sa scoped to the
# triage SA resource only — bounds the impersonation blast radius.
resource "google_service_account_iam_member" "tb_calendar_ingester_impersonate_triage" {
  # Reference the SA resource (not a hand-built path string) so Terraform
  # tracks the implicit dependency and the binding can't be applied before
  # the SA exists. Same resolved value; cleaner graph. (ADR 0064)
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_calendar_ingester_sa.email}"
}

# Read airtable_replica.accounts for the HIPAA domain set.
resource "google_bigquery_dataset_iam_member" "tb_calendar_ingester_replica_reader" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_calendar_ingester_sa.email}"
}

# Write into agent_outputs.notes (MERGE) + read existing rows for the
# external_id-based dedup pre-check.
resource "google_bigquery_dataset_iam_member" "tb_calendar_ingester_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_calendar_ingester_sa.email}"
}

# Write per-invocation audit rows.
resource "google_bigquery_dataset_iam_member" "tb_calendar_ingester_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_calendar_ingester_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  calendar_ingester_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/calendar-ingester:${var.calendar_ingester_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_calendar_ingester" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-calendar-ingester"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_calendar_ingester_sa.email
      timeout         = "1800s"
      max_retries     = 0

      containers {
        image = local.calendar_ingester_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "CALENDAR_DWD_TARGET_PRINCIPAL"
          value = "asb-agent-triage-sa@${var.brain_project_id}.iam.gserviceaccount.com"
        }
        env {
          name  = "CALENDAR_DWD_SUBJECT"
          value = var.calendar_ingester_dwd_subject
        }
        env {
          name  = "CALENDAR_IDS"
          value = var.calendar_ingester_calendars
        }
        env {
          name  = "CALENDAR_LOOKBACK_DAYS"
          value = var.calendar_ingester_lookback_days
        }
        env {
          name  = "CALENDAR_LOOKAHEAD_DAYS"
          value = var.calendar_ingester_lookahead_days
        }
        env {
          name  = "CALENDAR_VERTEX_LOCATION"
          value = var.region
        }
        env {
          name  = "CALENDAR_NOTES_SCOPE"
          value = var.calendar_ingester_notes_scope
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.tb_calendar_ingester_role,
    google_service_account_iam_member.tb_calendar_ingester_impersonate_triage,
    google_bigquery_dataset_iam_member.tb_calendar_ingester_replica_reader,
    google_bigquery_dataset_iam_member.tb_calendar_ingester_outputs_editor,
    google_bigquery_dataset_iam_member.tb_calendar_ingester_audit_writer,
    google_bigquery_table.notes,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "calendar_ingester_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_calendar_ingester.project
  location = google_cloud_run_v2_job.tb_calendar_ingester.location
  name     = google_cloud_run_v2_job.tb_calendar_ingester.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_calendar_ingester_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily 06:30 PT (after notes_ingestor at 06:00 PT, before
# morning brief at 07:25 PT)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_calendar_ingester_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-calendar-ingester-daily"
  schedule  = var.calendar_ingester_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the Calendar ingester daily (ADR 0046). 13:30 UTC = 06:30 PT. Runs after notes_ingestor (06:00 PT) so the calendar context is in agent_outputs.notes by the morning brief tick at 07:25 PT."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_calendar_ingester.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_calendar_ingester_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.calendar_ingester_scheduler_invoker]
}
