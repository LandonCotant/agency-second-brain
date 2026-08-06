# WS-G Librarian — Cloud Run Job + daily Cloud Scheduler (ADR 0044, plan Phase D).
#
# Topology mirrors notes_ingestor.tf — a dedicated SA with its own custom
# role, BQ dataset bindings, and a separate invoker SA holding only
# run.invoker. The Librarian moves files between user-shared Drive
# folders via ADC + the user's Editor share (ADR 0044 §1) — no DWD,
# no impersonation. ADR 0027 §2's "one DWD-grantable SA, two scopes"
# invariant is preserved.
#
# Differences from notes_ingestor:
#   - NO Pub/Sub publisher binding. The Librarian does not publish to
#     asb-triage-input — Drop-folder files are sort-only, never
#     auto-classified into Triage Inbox (the user's clean rule:
#     QuickNotes for actionable, Drop for "just file it").
#   - NO writes to agent_outputs.notes. The Notes Ingestor still owns
#     all corpus writes; the Librarian only writes agent_outputs.notes_links
#     (semantic neighbors) + agent_audit_log.events. Move targets land
#     under Brain/Areas/, which Notes Ingestor already polls — moved
#     files re-ingest with note_kind='area' on the next daily tick.
#
# Image lifecycle mirrors notes_ingestor: bootstrap tag, manual rebuilds
# via cloudbuild.librarian.yaml, lifecycle.ignore_changes = [image].

variable "librarian_image_tag" {
  description = "Container tag for asb-librarian. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.librarian.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "librarian_schedule" {
  description = "Cloud Scheduler cron expression for the daily Librarian tick (ADR 0044). Default 0 12 * * * (12:00 UTC = 5:00 PT) so it runs before the 6:00 PT notes-ingestor tick — Drop files get classified+moved BEFORE the daily ingest sees them, so they ingest with their final note_kind (area/resource) instead of inbox."
  type        = string
  default     = "0 12 * * *"
}

variable "librarian_timezone" {
  description = "IANA tz for librarian_schedule. Cloud Scheduler handles DST."
  type        = string
  default     = "America/Los_Angeles"
}

variable "brain_inbox_drop_folder_id" {
  description = "Drive folder id of 'Brain/Inbox/Drop/' (ADR 0044 + plan Phase D). Files dropped here are classified by Gemini + moved into the right Brain/Areas/<topic>/ folder by the Librarian. Empty disables Drop-folder sweeping."
  type        = string
  default     = ""
}

variable "librarian_quicknotes_age_days" {
  description = "QuickNotes files older than this many days are eligible for Librarian sorting. Bounds the user's window to manually file fresh captures before the agent classifies them."
  type        = string
  default     = "14"
}

variable "librarian_max_per_tick" {
  description = "Cap on files processed per Librarian tick. Bounds Gemini classifier spend if the user bulk-drops a backlog."
  type        = string
  default     = "50"
}

variable "librarian_confidence_threshold" {
  description = "Minimum LLM self-rated confidence for a file to land in the classified destination; below this it goes to Brain/Areas/_uncategorized/. Default 0.6 errs toward the safe fallback (ADR 0044 §risk #5)."
  type        = string
  default     = "0.6"
}

variable "librarian_link_top_k" {
  description = "Top-K semantic neighbors the linker writes into agent_outputs.notes_links per file. Doubles via bidirectional inserts (source→target AND target→source)."
  type        = string
  default     = "3"
}

variable "librarian_link_cosine_threshold" {
  description = "Minimum cosine similarity for the linker to insert a notes_links row. ADR 0038 §2 default 0.78. Bumpable to 0.82 if early-corpus runs produce noisy links."
  type        = string
  default     = "0.78"
}

variable "librarian_dossier_filename" {
  description = "Filename the linker looks for inside each <topic>/ folder when wiring the auto-edited '## Related' section. Default 'dossier.gdoc'; empty disables the dossier-edit step entirely."
  type        = string
  default     = "dossier.gdoc"
}

variable "librarian_dest_roots" {
  description = "Phase G + ADR 0054 — comma-separated list of destination roots. Each entry is either 'label:folder_id' (legacy; bucket defaults to 'areas') or 'bucket=label:folder_id' (bucket ∈ {areas, resources}). Each root is walked to build the candidate set for classification; bucket drives note_kind downstream (areas → 'area', resources → 'resource'). Empty falls back to the legacy single-root BRAIN_AREAS_FOLDER_ID. Recommended: 'brain:<Brain/05_AREAS id>,resources=resources:<Brain/03_RESOURCES id>,clients:<the agency/05_CLIENTS id>'."
  type        = string
  default     = ""
}

variable "librarian_excluded_folder_names" {
  description = "Phase G — comma-separated list of folder display names to exclude from the candidate set (case-insensitive). Useful for keeping sensitive folders out of the classifier (e.g. '02_FINANCE & ACCOUNTING'). '_uncategorized' is always excluded by default."
  type        = string
  default     = ""
}

variable "brain_galaxy_folder_id" {
  description = "ADR 0054 §2 — Drive folder id of 'Brain/05_GALAXY/'. When set, the Librarian sweeps Galaxy recursively per tick and indexes each file with note_kind='galaxy'. No move, no classifier — the user's folder structure is preserved as-is. Empty disables the sweep silently."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "tb_librarian_sa" {
  project      = var.brain_project_id
  account_id   = "asb-librarian-sa"
  display_name = "Agency Second Brain Librarian"
  description  = "Runs the WS-G Librarian (ADR 0044). Lists + moves Drive files between Brain/Inbox/Drop|QuickNotes/ and Brain/Areas/<topic>/. Writes notes_links + audit log. No DWD; folder access via folder-level Editor share by the user."
}

resource "google_service_account" "tb_librarian_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-librarian-invoker"
  display_name = "Cloud Scheduler invoker for asb-librarian"
  description  = "Holds run.invoker on the Librarian Cloud Run Job. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_librarian" {
  project     = var.brain_project_id
  role_id     = "tbLibrarian"
  title       = "Agency Second Brain Librarian"
  description = "Project-level permissions for the WS-G Librarian. BQ dataset access is granted separately. Drive access is via folder-level user share. ADR 0044."
  stage       = "GA"
  permissions = [
    # BigQuery query exec for VECTOR_SEARCH + linker INSERTs.
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Vertex AI Gemini 2.5 Flash classifier + text-embedding-005.
    "aiplatform.endpoints.predict",
    # Agent Observability traces.
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_librarian_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_librarian.id
  member  = "serviceAccount:${google_service_account.tb_librarian_sa.email}"
}

# Read agent_outputs.notes (corpus + embeddings) + write notes_links.
# dataEditor covers both — the linker SELECTs from notes and INSERTs
# into notes_links in the same dataset.
resource "google_bigquery_dataset_iam_member" "tb_librarian_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_librarian_sa.email}"
}

# Write per-invocation audit rows.
resource "google_bigquery_dataset_iam_member" "tb_librarian_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_librarian_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  librarian_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/librarian:${var.librarian_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_librarian" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-librarian"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_librarian_sa.email
      # Daily cadence; one tick processes up to LIBRARIAN_MAX_PER_TICK
      # files. 30 minutes is plenty of headroom for classify + move +
      # link across a typical day's drops (most ticks finish in seconds
      # because Drop is empty).
      timeout     = "1800s"
      max_retries = 0

      containers {
        image = local.librarian_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "BRAIN_AREAS_FOLDER_ID"
          value = var.brain_areas_folder_id
        }
        env {
          name  = "BRAIN_INBOX_DROP_FOLDER_ID"
          value = var.brain_inbox_drop_folder_id
        }
        env {
          name  = "BRAIN_INBOX_QUICKNOTES_FOLDER_ID"
          value = var.brain_inbox_quicknotes_folder_id
        }
        env {
          name  = "LIBRARIAN_QUICKNOTES_AGE_DAYS"
          value = var.librarian_quicknotes_age_days
        }
        env {
          name  = "LIBRARIAN_MAX_PER_TICK"
          value = var.librarian_max_per_tick
        }
        env {
          name  = "LIBRARIAN_CONFIDENCE_THRESHOLD"
          value = var.librarian_confidence_threshold
        }
        env {
          name  = "LIBRARIAN_LINK_TOP_K"
          value = var.librarian_link_top_k
        }
        env {
          name  = "LIBRARIAN_LINK_COSINE_THRESHOLD"
          value = var.librarian_link_cosine_threshold
        }
        env {
          name  = "LIBRARIAN_DOSSIER_FILENAME"
          value = var.librarian_dossier_filename
        }
        env {
          name  = "LIBRARIAN_DEST_ROOTS"
          value = var.librarian_dest_roots
        }
        env {
          name  = "LIBRARIAN_EXCLUDED_FOLDER_NAMES"
          value = var.librarian_excluded_folder_names
        }
        env {
          name  = "BRAIN_GALAXY_FOLDER_ID"
          value = var.brain_galaxy_folder_id
        }
        env {
          name  = "LIBRARIAN_SA_EMAIL"
          value = google_service_account.tb_librarian_sa.email
        }
        env {
          name  = "VERTEX_LOCATION"
          value = var.region
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi" # Gemini multimodal extraction holds the file body in-memory.
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.tb_librarian_role,
    google_bigquery_dataset_iam_member.tb_librarian_outputs_editor,
    google_bigquery_dataset_iam_member.tb_librarian_audit_writer,
    google_bigquery_table.notes,
    google_bigquery_table.notes_links,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "librarian_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_librarian.project
  location = google_cloud_run_v2_job.tb_librarian.location
  name     = google_cloud_run_v2_job.tb_librarian.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_librarian_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily Librarian tick
# ---------------------------------------------------------------------------
# Lands paused so the first scheduled tick can't fire against a
# placeholder/bootstrap image. Rollout: build image → update tfvars
# tag → targeted apply → manual smoke fire via gcloud → unpause via
# `gcloud scheduler jobs resume asb-librarian-daily`.

resource "google_cloud_scheduler_job" "tb_librarian_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-librarian-daily"
  schedule  = var.librarian_schedule
  time_zone = var.librarian_timezone

  description = "Daily Librarian tick (ADR 0044). 5:00 PT — runs before notes-ingestor's 6:00 PT pickup so Drop files land in Brain/Areas/ before the daily ingest sees them, picking up the right note_kind on first ingestion."

  paused = true

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_librarian.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_librarian_invoker_sa.email
    }
  }

  # Once unpaused via gcloud after smoke, ignore TF flipping it back.
  lifecycle {
    ignore_changes = [paused]
  }

  depends_on = [google_cloud_run_v2_job_iam_member.librarian_scheduler_invoker]
}
