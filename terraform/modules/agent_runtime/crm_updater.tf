# ADR 0047 — CRM Auto-updater (Workstream B PR B2).
#
# Daily Cloud Run Job that reads `secondbrain`-labeled emails,
# extracts task / contact / account drafts, writes them to Airtable
# as drafts (drafts-only per PRD §4.7).
#
# DWD: REUSES `asb-agent-triage-sa` (ADR 0027 §3 invariant), with two
# new scopes added to the DWD allowlist by ADR 0047:
#   - gmail.readonly  (read labeled message bodies)
#   - gmail.modify    (apply secondbrain-processed dedup label)
# The grant in the Workspace Admin Console is a one-time manual step
# documented in docs/runbooks/dwd_scope_expansion_2026-05.md.

variable "crm_updater_image_tag" {
  description = "Container tag for asb-crm-updater (ADR 0047). 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.crm-updater.yaml has built and pushed a real image."
  type        = string
  default     = "bootstrap"
}

variable "crm_updater_schedule" {
  description = "Cloud Scheduler cron for the CRM Auto-updater. Default '15 13 * * *' (daily 13:15 UTC = 06:15 PT). Runs before notes_ingestor (06:00 PT) — wait, runs AFTER. Sequence: 06:00 notes, 06:15 crm-updater, 06:30 calendar, 07:25 morning-brief. crm-updater is independent of the others; ordering doesn't matter beyond cost-of-day pacing."
  type        = string
  default     = "15 13 * * *"
}

variable "crm_updater_dwd_subject" {
  description = "DWD subject for gmail.readonly + gmail.modify. Defaults to owner@example.com — the inbox owner whose labeled mail the Auto-updater reads."
  type        = string
  default     = "owner@example.com"
}

variable "crm_updater_label" {
  description = "Gmail label applied by the operator's existing automation that gates Auto-updater scope. Default 'secondbrain'."
  type        = string
  default     = "secondbrain"
}

variable "crm_updater_processed_label" {
  description = "Gmail label applied by the Auto-updater after successful drafting. Used for idempotency on retry."
  type        = string
  default     = "secondbrain-processed"
}

variable "crm_updater_max_messages_per_run" {
  description = "Cap on emails processed per scheduler tick. Bounds Vertex spend if a backlog builds up."
  type        = string
  default     = "50"
}

variable "crm_updater_inbox_project_record_id" {
  description = "Airtable Projects record id used as the 'Triage Inbox' default Project for tasks drafted by the Auto-updater. Reuses TB_TRIAGE_INBOX_PROJECT_ID. Empty disables the scheduler (the env var is required at runtime)."
  type        = string
  default     = ""
}

variable "crm_updater_airtable_pat_secret_id" {
  description = "Secret Manager short name holding the Airtable PAT for CRM writes (POST Tasks + PATCH Contacts/Accounts.Pending Updates). May share the same secret as the Triage Tasks PAT."
  type        = string
  default     = "airtable-tasks-write-pat-prod"
}

variable "crm_updater_disable_notes_write" {
  description = "ADR 0049 — kill switch for the Gmail-into-corpus side-effect writer. Empty string (default) leaves the corpus write ON. Set to '1' or 'true' to disable without rebuilding the image (e.g. if Vertex embeddings cost spikes)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_crm_updater_sa" {
  project      = var.brain_project_id
  account_id   = "asb-crm-updater-sa"
  display_name = "Agency CRM Auto-updater"
  description  = "ADR 0047. Reads secondbrain-labeled emails via DWD impersonation; drafts Airtable Tasks + Pending Updates."
}

resource "google_service_account" "tb_crm_updater_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-crm-updater-invoker"
  display_name = "Cloud Scheduler invoker for asb-crm-updater"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_crm_updater" {
  project     = var.brain_project_id
  role_id     = "tbCrmUpdater"
  title       = "Agency CRM Auto-updater"
  description = "Project-level permissions for the CRM Auto-updater. ADR 0047. No predefined high-privilege roles."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    "aiplatform.endpoints.predict",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_crm_updater_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_crm_updater.id
  member  = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

# Resource-level grant: asb-crm-updater-sa impersonates asb-agent-triage-sa
# for the new gmail.readonly + gmail.modify scopes (ADR 0047 §3).
resource "google_service_account_iam_member" "tb_crm_updater_impersonate_triage" {
  # Reference the SA resource (not a hand-built path string) so Terraform
  # tracks the implicit dependency and the binding can't be applied before
  # the SA exists. Same resolved value; cleaner graph. (ADR 0064)
  service_account_id = google_service_account.tb_agent_triage_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

# Read airtable_replica.{accounts, contacts} for HIPAA + entity allowlists.
resource "google_bigquery_dataset_iam_member" "tb_crm_updater_replica_reader" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

# Write crm_updater_runs checkpoint + dataEditor on agent_outputs (the
# checkpoint table lives there).
resource "google_bigquery_dataset_iam_member" "tb_crm_updater_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_crm_updater_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

# Airtable PAT access.
resource "google_secret_manager_secret_iam_member" "tb_crm_updater_pat_accessor" {
  count     = var.crm_updater_airtable_pat_secret_id != "" ? 1 : 0
  project   = var.brain_project_id
  secret_id = var.crm_updater_airtable_pat_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_crm_updater_sa.email}"
}

# ---------------------------------------------------------------------------
# crm_updater_runs checkpoint table
# ---------------------------------------------------------------------------
# One row per Cloud Run Job execution. The Auto-updater reads the most
# recent successful row to determine the resumption cursor (the
# secondbrain-processed label is the actual dedup; the table is run-history
# for observability + cost tracking).

resource "google_bigquery_table" "crm_updater_runs" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = "crm_updater_runs"

  description = "ADR 0047. One row per CRM Auto-updater execution. Run-history + per-tick stats. Most-recent-success row is the resumption cursor."

  time_partitioning {
    type          = "DAY"
    field         = "started_at"
    expiration_ms = 31536000000 # 365 days
  }
  clustering = ["success"]

  schema = jsonencode([
    { name = "run_id", type = "STRING", mode = "REQUIRED",
    description = "UUID-ish run id (`crmu-<hex16>`). Generated by dedup.new_run_id()." },
    { name = "started_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Run start (UTC). Partition column." },
    { name = "ended_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Run end (UTC)." },
    { name = "history_id_after", type = "STRING", mode = "NULLABLE",
    description = "Highest Gmail historyId observed during the run. Reserved for future history-API integration." },
    { name = "messages_processed", type = "INT64", mode = "REQUIRED",
    description = "Count of messages the agent invoked on (post-HIPAA-filter)." },
    { name = "drafts_created", type = "INT64", mode = "REQUIRED",
    description = "Sum of drafted Tasks + appended Pending Updates blocks." },
    { name = "errors", type = "INT64", mode = "REQUIRED",
    description = "Count of per-message processing errors." },
    { name = "success", type = "BOOL", mode = "REQUIRED",
    description = "TRUE when errors == 0. Cluster column." },
  ])

  deletion_protection = true

  labels = {
    workstream = "agent_runtime"
    component  = "outputs"
  }
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  crm_updater_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/crm-updater:${var.crm_updater_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_crm_updater" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-crm-updater"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_crm_updater_sa.email
      timeout         = "1800s"
      max_retries     = 0

      containers {
        image = local.crm_updater_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "CRM_UPDATER_DWD_TARGET_PRINCIPAL"
          value = "asb-agent-triage-sa@${var.brain_project_id}.iam.gserviceaccount.com"
        }
        env {
          name  = "CRM_UPDATER_DWD_SUBJECT"
          value = var.crm_updater_dwd_subject
        }
        env {
          name  = "CRM_UPDATER_LABEL"
          value = var.crm_updater_label
        }
        env {
          name  = "CRM_UPDATER_PROCESSED_LABEL"
          value = var.crm_updater_processed_label
        }
        env {
          name  = "CRM_UPDATER_MAX_MESSAGES_PER_RUN"
          value = var.crm_updater_max_messages_per_run
        }
        env {
          name  = "CRM_UPDATER_INBOX_PROJECT_RECORD_ID"
          value = var.crm_updater_inbox_project_record_id
        }
        env {
          name  = "AIRTABLE_OPS_BASE_ID"
          value = var.airtable_base_id
        }
        env {
          name  = "AIRTABLE_TASKS_WRITE_PAT_SECRET_ID"
          value = var.crm_updater_airtable_pat_secret_id
        }
        env {
          name  = "CRM_UPDATER_SA_EMAIL"
          value = google_service_account.tb_crm_updater_sa.email
        }
        # ADR 0049 — Vertex location for the text-embedding-005 client
        # used by the Gmail-into-corpus side-effect writer.
        env {
          name  = "VERTEX_LOCATION"
          value = var.region
        }
        env {
          name  = "EMBEDDING_MODEL"
          value = "text-embedding-005"
        }
        # ADR 0049 — kill switch. Default empty = corpus write ON.
        # Set to "1" / "true" to disable (rolls back Phase H2 without a
        # redeploy).
        env {
          name  = "CRM_UPDATER_DISABLE_NOTES_WRITE"
          value = var.crm_updater_disable_notes_write
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
    google_project_iam_member.tb_crm_updater_role,
    google_service_account_iam_member.tb_crm_updater_impersonate_triage,
    google_bigquery_dataset_iam_member.tb_crm_updater_replica_reader,
    google_bigquery_dataset_iam_member.tb_crm_updater_outputs_editor,
    google_bigquery_dataset_iam_member.tb_crm_updater_audit_writer,
    google_bigquery_table.crm_updater_runs,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "crm_updater_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_crm_updater.project
  location = google_cloud_run_v2_job.tb_crm_updater.location
  name     = google_cloud_run_v2_job.tb_crm_updater.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_crm_updater_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily 06:15 PT (ADR 0047)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_crm_updater_daily" {
  count = var.crm_updater_inbox_project_record_id != "" ? 1 : 0

  project   = var.brain_project_id
  region    = var.region
  name      = "asb-crm-updater-daily"
  schedule  = var.crm_updater_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the CRM Auto-updater daily (ADR 0047). 13:15 UTC = 06:15 PT. Reads `secondbrain`-labeled email; drafts Airtable Tasks + Pending Updates blocks. Disabled (count=0) when CRM_UPDATER_INBOX_PROJECT_RECORD_ID is empty — the runtime requires that env var, and we don't want the scheduler to fire into a broken Job."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_crm_updater.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_crm_updater_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.crm_updater_scheduler_invoker]
}
