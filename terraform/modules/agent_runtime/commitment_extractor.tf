# ADR 0069 — Commitment extractor (action memory): Cloud Run Job + daily
# Cloud Scheduler.
#
# A single post-processor over agent_outputs.notes (all four sources already
# land there per ADR 0049 / 0046 / 0031). Per tick it scans new, non-HIPAA,
# in-scope notes, Gemini-extracts commitments, resolves account_id, and writes
# agent_outputs.commitments. Drafts-only (ADR 0069 §6) — no Gmail, no Airtable,
# no source-table writes — so NO DWD impersonation and NO Chat secret, unlike
# brag-spotter. Topology otherwise mirrors brag-spotter (ADR 0043): new runtime
# SA + dedicated invoker SA + image-tag lifecycle ignore.

variable "commitment_extractor_image_tag" {
  description = "Container tag for asb-commitment-extractor (ADR 0069). 'bootstrap' is a placeholder so the first apply succeeds before cloudbuild.commitment-extractor.yaml builds a real one."
  type        = string
  default     = "bootstrap"
}

variable "commitment_extractor_schedule" {
  description = "Cloud Scheduler cron. Default '0 7 * * *' = 07:00 daily, after the ingestion jobs (notes 06:00, crm 06:15, calendar 06:30)."
  type        = string
  default     = "0 7 * * *"
}

variable "commitment_extractor_timezone" {
  description = "IANA tz for commitment_extractor_schedule."
  type        = string
  default     = "America/Los_Angeles"
}

variable "commitment_source_note_kinds" {
  description = "Comma-separated note_kinds the extractor scans. ADR 0069 §3 starts narrow; 'area' excluded to control noise."
  type        = string
  default     = "email,inbox,calendar_event,capture"
}

variable "commitment_min_confidence" {
  description = "Confidence floor [0,1]; commitments below this are dropped before write (ADR 0069 §3)."
  type        = string
  default     = "0.6"
}

variable "commitment_stale_days" {
  description = "A commitment with no explicit due_date goes overdue this many days after extraction (ADR 0069 §2). MUST match the MCP server's COMMITMENT_STALE_DAYS so open_commitments + Morning Brief agree."
  type        = string
  default     = "7"
}

variable "commitment_max_notes_per_tick" {
  description = "Cap on notes scanned per execution (cost + runtime guard)."
  type        = string
  default     = "200"
}

# ---------------------------------------------------------------------------
# BigQuery: agent_outputs.commitments (the action-memory table)
# ---------------------------------------------------------------------------

resource "google_bigquery_table" "commitments" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  table_id   = "commitments"

  description = "Extracted action commitments — who promised what, by when, which direction. Populated by asb-commitment-extractor from agent_outputs.notes. ADR 0069."

  time_partitioning {
    type          = "DAY"
    field         = "extracted_at"
    expiration_ms = 63072000000 # 730 days, mirrors triaged_items per ADR 0024
  }
  clustering = ["account_id", "status"]

  schema = jsonencode([
    { name = "commitment_id", type = "STRING", mode = "REQUIRED",
    description = "UUIDv4." },
    { name = "extracted_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "When the extractor discovered this commitment (UTC). Partition column." },
    { name = "source_note_id", type = "STRING", mode = "REQUIRED",
    description = "Joins agent_outputs.notes.note_id — provenance / evidence link." },
    { name = "source_note_kind", type = "STRING", mode = "REQUIRED",
    description = "email | inbox | calendar_event | capture." },
    { name = "direction", type = "STRING", mode = "REQUIRED",
    description = "mine (the operator promised) | theirs (promised to the operator)." },
    { name = "counterparty_email", type = "STRING", mode = "NULLABLE",
    description = "The other party's email (best-effort)." },
    { name = "counterparty_name", type = "STRING", mode = "NULLABLE",
    description = "The other party's name (best-effort)." },
    { name = "account_id", type = "STRING", mode = "NULLABLE",
    description = "Airtable Accounts rec id resolved from counterparty email; NULL if unresolved. Cluster column." },
    { name = "commitment_text", type = "STRING", mode = "REQUIRED",
    description = "Concise paraphrase of what was promised." },
    { name = "due_date", type = "DATE", mode = "NULLABLE",
    description = "Explicit or inferred due date; NULL when none stated (overdue logic falls back to extracted_at + COMMITMENT_STALE_DAYS)." },
    { name = "status", type = "STRING", mode = "REQUIRED",
    description = "open | done | cancelled. Extractor only writes open. Cluster column." },
    { name = "confidence", type = "FLOAT64", mode = "REQUIRED",
    description = "[0,1] that this is a genuine commitment, not noise." },
    { name = "reasoning", type = "STRING", mode = "REQUIRED",
    description = "Why the extractor flagged it." },
    { name = "agent_run_id", type = "STRING", mode = "REQUIRED",
    description = "Joins agent_audit_log.events for traceability." },
  ])

  deletion_protection = true

  labels = {
    workstream = "agent_runtime"
    component  = "outputs"
  }
}

# ---------------------------------------------------------------------------
# BigQuery: agent_state.commitment_extractor_watermark (scan cursor)
# ---------------------------------------------------------------------------

resource "google_bigquery_table" "commitment_extractor_watermark" {
  project     = var.brain_project_id
  dataset_id  = google_bigquery_dataset.agent_state.dataset_id
  table_id    = "commitment_extractor_watermark"
  description = "Insert-only scan cursor for asb-commitment-extractor — max notes.ingested_at processed. Readers take the most-recent row per cursor_name. ADR 0069 §4."
  clustering  = ["cursor_name"]

  schema = jsonencode([
    { name = "cursor_name", type = "STRING", mode = "REQUIRED",
    description = "Logical cursor id; constant 'commitment-extractor' in v1. Cluster column." },
    { name = "last_ingested_at_seen", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Highest notes.ingested_at processed in the most-recent successful tick. Drives the next tick's `ingested_at > <cursor>` filter." },
    { name = "updated_at", type = "TIMESTAMP", mode = "REQUIRED",
    description = "Wall-clock write time. Tie-breaker for most-recent selection." },
  ])

  # No deletion_protection: pure operational state, recreatable from the
  # commitments table if ever lost (NOT IN commitments guard backstops dedup).
  deletion_protection = false

  labels = {
    workstream = "agent_runtime"
    component  = "state"
  }
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_commitment_extractor_sa" {
  project = var.brain_project_id
  # account_id capped at 30 chars by GCP, so the stem is "asb-commitment-extract-"
  # (the Job / scheduler / role names keep the full "commitment-extractor").
  account_id   = "asb-commitment-extract-sa"
  display_name = "Agency Commitment Extractor"
  description  = "Runs the ADR 0069 commitment extractor daily Job. Reads agent_outputs.notes + airtable_replica; writes agent_outputs.commitments + agent_state watermark. Drafts-only — no DWD, no Gmail, no Airtable writes. Observability via Cloud Logging structured logs."
}

resource "google_service_account" "tb_commitment_extractor_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-commitment-extract-invoker"
  display_name = "Cloud Scheduler invoker for asb-commitment-extractor"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + project binding
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_commitment_extractor" {
  project     = var.brain_project_id
  role_id     = "tbCommitmentExtractor"
  title       = "Agency Commitment Extractor"
  description = "Project-level permissions for the ADR 0069 commitment extractor. BQ dataset access granted separately via google_bigquery_dataset_iam_member."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Vertex Gemini structured response (gemini-2.5-flash + response_schema)
    "aiplatform.endpoints.predict",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_commitment_extractor_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_commitment_extractor.id
  member  = "serviceAccount:${google_service_account.tb_commitment_extractor_sa.email}"
}

# ---------------------------------------------------------------------------
# Dataset bindings
# ---------------------------------------------------------------------------

# Read notes + write commitments (same dataset).
resource "google_bigquery_dataset_iam_member" "tb_commitment_extractor_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_commitment_extractor_sa.email}"
}

# Read + write the scan watermark.
resource "google_bigquery_dataset_iam_member" "tb_commitment_extractor_state_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_state.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_commitment_extractor_sa.email}"
}

# Resolve counterparty email -> account_id via contacts replica (read-only).
resource "google_bigquery_dataset_iam_member" "tb_commitment_extractor_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_commitment_extractor_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  commitment_extractor_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/commitment-extractor:${var.commitment_extractor_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_commitment_extractor" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-commitment-extractor"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_commitment_extractor_sa.email
      timeout         = "900s"
      max_retries     = 0

      containers {
        image = local.commitment_extractor_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRAIN_VERTEX_LOCATION"
          value = var.region
        }
        env {
          name  = "COMMITMENT_SOURCE_NOTE_KINDS"
          value = var.commitment_source_note_kinds
        }
        env {
          name  = "COMMITMENT_MIN_CONFIDENCE"
          value = var.commitment_min_confidence
        }
        env {
          name  = "COMMITMENT_STALE_DAYS"
          value = var.commitment_stale_days
        }
        env {
          name  = "COMMITMENT_MAX_NOTES_PER_TICK"
          value = var.commitment_max_notes_per_tick
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
    google_project_iam_member.tb_commitment_extractor_role,
    google_bigquery_dataset_iam_member.tb_commitment_extractor_outputs_editor,
    google_bigquery_dataset_iam_member.tb_commitment_extractor_state_editor,
    google_bigquery_dataset_iam_member.tb_commitment_extractor_replica_viewer,
    google_bigquery_table.commitments,
    google_bigquery_table.commitment_extractor_watermark,
    google_bigquery_table.notes,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "commitment_extractor_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_commitment_extractor.project
  location = google_cloud_run_v2_job.tb_commitment_extractor.location
  name     = google_cloud_run_v2_job.tb_commitment_extractor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_commitment_extractor_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — 07:00 PT daily
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_commitment_extractor_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-commitment-extractor-daily"
  schedule  = var.commitment_extractor_schedule
  time_zone = var.commitment_extractor_timezone

  description = "Daily kick-off for the commitment extractor Cloud Run Job. ADR 0069."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_commitment_extractor.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_commitment_extractor_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.commitment_extractor_scheduler_invoker]
}
