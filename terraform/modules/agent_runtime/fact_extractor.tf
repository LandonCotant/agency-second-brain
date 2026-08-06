# ADR 0070 — Fact extractor (bi-temporal factual memory): Cloud Run Job +
# daily Cloud Scheduler.
#
# Mirrors the ADR 0069 commitment extractor: a single post-processor over
# agent_outputs.notes that Gemini-extracts entity-attribute facts into the
# append-only agent_outputs.facts log. Drafts-only — no DWD, no Gmail, no
# Airtable writes. Append-only (no UPDATE) — current-vs-superseded validity is
# derived at read time (ADR 0070 §3), sidestepping streaming-buffer DML.

variable "fact_extractor_image_tag" {
  description = "Container tag for asb-fact-extractor (ADR 0070). 'bootstrap' is a placeholder so the first apply succeeds before cloudbuild.fact-extractor.yaml builds a real one."
  type        = string
  default     = "bootstrap"
}

variable "fact_extractor_schedule" {
  description = "Cloud Scheduler cron. Default '15 7 * * *' = 07:15 daily, after the commitment extractor (07:00)."
  type        = string
  default     = "15 7 * * *"
}

variable "fact_extractor_timezone" {
  description = "IANA tz for fact_extractor_schedule."
  type        = string
  default     = "America/Los_Angeles"
}

variable "fact_source_note_kinds" {
  description = "Comma-separated note_kinds the extractor scans (ADR 0070 §4). 'area'/'galaxy' excluded — galaxy is Airtable-derived."
  type        = string
  default     = "email,inbox,calendar_event,capture"
}

variable "fact_min_confidence" {
  description = "Confidence floor [0,1]; facts below this are dropped before write (ADR 0070 §6). Higher than commitments' 0.6 — facts read as truth."
  type        = string
  default     = "0.7"
}

variable "fact_max_notes_per_tick" {
  description = "Cap on notes scanned per execution (cost + runtime guard)."
  type        = string
  default     = "200"
}

# ---------------------------------------------------------------------------
# BigQuery: agent_outputs.facts (append-only event log)
# ---------------------------------------------------------------------------

resource "google_bigquery_table" "facts" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = "facts"

  description = "Append-only entity-attribute fact log (who/what attribute = value, since when). Populated by asb-fact-extractor from agent_outputs.notes; current-vs-superseded derived at read time. ADR 0070."

  time_partitioning {
    type          = "DAY"
    field         = "extracted_at"
    expiration_ms = 63072000000 # 730 days, mirrors triaged_items per ADR 0024
  }
  clustering = ["entity_id", "predicate"]

  schema = jsonencode([
    { name = "fact_id", type = "STRING", mode = "REQUIRED",
    description = "UUIDv4." },
    { name = "extracted_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Transaction time — when recorded (UTC). Partition column." },
    { name = "observed_date", type = "DATE", mode = "REQUIRED",
    description = "Event time / valid_from — when the fact became true (best-effort; defaults to source note date)." },
    { name = "entity_id", type = "STRING", mode = "NULLABLE",
    description = "Airtable rec id (account or contact); NULL if unresolved. Cluster column." },
    { name = "entity_type", type = "STRING", mode = "NULLABLE",
    description = "account | contact." },
    { name = "entity_name", type = "STRING", mode = "REQUIRED",
    description = "Resolved or extracted display name." },
    { name = "predicate", type = "STRING", mode = "REQUIRED",
    description = "Normalized snake_case attribute key. Cluster column." },
    { name = "value", type = "STRING", mode = "REQUIRED",
    description = "The attribute value." },
    { name = "source_note_id", type = "STRING", mode = "REQUIRED",
    description = "Joins agent_outputs.notes.note_id — evidence link." },
    { name = "source_note_kind", type = "STRING", mode = "REQUIRED",
    description = "email | inbox | calendar_event | capture." },
    { name = "confidence", type = "FLOAT64", mode = "REQUIRED",
    description = "[0,1] that this is a genuine durable attribute." },
    { name = "agent_run_id", type = "STRING", mode = "REQUIRED",
    description = "Traceability." },
  ])

  deletion_protection = true

  labels = {
    workstream = "agent_runtime"
    component  = "outputs"
  }
}

# ---------------------------------------------------------------------------
# BigQuery: agent_state.fact_extractor_watermark (scan cursor)
# ---------------------------------------------------------------------------

resource "google_bigquery_table" "fact_extractor_watermark" {
  project     = var.brain_project_id
  dataset_id  = google_bigquery_dataset.agent_state.dataset_id
  table_id    = "fact_extractor_watermark"
  description = "Insert-only scan cursor for asb-fact-extractor — max notes.ingested_at processed. ADR 0070 §4."
  clustering  = ["cursor_name"]

  schema = jsonencode([
    { name = "cursor_name", type = "STRING", mode = "REQUIRED",
    description = "Logical cursor id; constant 'fact-extractor' in v1. Cluster column." },
    { name = "last_ingested_at_seen", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Highest notes.ingested_at processed in the most-recent successful tick." },
    { name = "updated_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Wall-clock write time. Tie-breaker for most-recent selection." },
  ])

  deletion_protection = false

  labels = {
    workstream = "agent_runtime"
    component  = "state"
  }
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_fact_extractor_sa" {
  project      = var.brain_project_id
  account_id   = "asb-fact-extractor-sa"
  display_name = "Agency Fact Extractor"
  description  = "Runs the ADR 0070 fact extractor daily Job. Reads agent_outputs.notes + airtable_replica; writes agent_outputs.facts + agent_state watermark. Drafts-only — no DWD, no Gmail, no Airtable writes. Observability via Cloud Logging structured logs."
}

resource "google_service_account" "tb_fact_extractor_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-fact-extractor-invoker"
  display_name = "Cloud Scheduler invoker for asb-fact-extractor"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + project binding
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_fact_extractor" {
  project     = var.brain_project_id
  role_id     = "tbFactExtractor"
  title       = "Agency Fact Extractor"
  description = "Project-level permissions for the ADR 0070 fact extractor. BQ dataset access granted separately via google_bigquery_dataset_iam_member."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    "aiplatform.endpoints.predict",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_fact_extractor_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_fact_extractor.id
  member  = "serviceAccount:${google_service_account.tb_fact_extractor_sa.email}"
}

# ---------------------------------------------------------------------------
# Dataset bindings
# ---------------------------------------------------------------------------

resource "google_bigquery_dataset_iam_member" "tb_fact_extractor_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_fact_extractor_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_fact_extractor_state_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_state.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_fact_extractor_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "tb_fact_extractor_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_fact_extractor_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  fact_extractor_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/fact-extractor:${var.fact_extractor_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_fact_extractor" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-fact-extractor"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_fact_extractor_sa.email
      timeout         = "900s"
      max_retries     = 0

      containers {
        image = local.fact_extractor_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRAIN_VERTEX_LOCATION"
          value = var.region
        }
        env {
          name  = "FACT_SOURCE_NOTE_KINDS"
          value = var.fact_source_note_kinds
        }
        env {
          name  = "FACT_MIN_CONFIDENCE"
          value = var.fact_min_confidence
        }
        env {
          name  = "FACT_MAX_NOTES_PER_TICK"
          value = var.fact_max_notes_per_tick
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
    google_project_iam_member.tb_fact_extractor_role,
    google_bigquery_dataset_iam_member.tb_fact_extractor_outputs_editor,
    google_bigquery_dataset_iam_member.tb_fact_extractor_state_editor,
    google_bigquery_dataset_iam_member.tb_fact_extractor_replica_viewer,
    google_bigquery_table.facts,
    google_bigquery_table.fact_extractor_watermark,
    google_bigquery_table.notes,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "fact_extractor_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_fact_extractor.project
  location = google_cloud_run_v2_job.tb_fact_extractor.location
  name     = google_cloud_run_v2_job.tb_fact_extractor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_fact_extractor_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — 07:15 PT daily
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_fact_extractor_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-fact-extractor-daily"
  schedule  = var.fact_extractor_schedule
  time_zone = var.fact_extractor_timezone

  description = "Daily kick-off for the fact extractor Cloud Run Job. ADR 0070."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_fact_extractor.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_fact_extractor_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.fact_extractor_scheduler_invoker]
}
