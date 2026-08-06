# ADR 0031 — Samsung Notes ingestor: Cloud Run Job + weekly scheduler.
#
# Topology mirrors triage_bridge (ADR 0019) and morning_brief (ADR 0029):
# Cloud Run Job + Cloud Scheduler + dedicated invoker SA holding only
# run.invoker. The job itself runs as a NEW SA, `asb-notes-ingestor-sa`,
# which is intentionally separate from `asb-agent-triage-sa` because
# (a) the ingestor needs Drive API access scoped to two specific user-
# shared folders — that authority is unrelated to triage classification,
# and (b) the smaller blast-radius matches PRD §4.2 SA topology.
#
# The Drive folder access model is OAuth-invisible: ADR 0031 §1 — the
# user shares each watched folder directly with this SA email, and the
# SA accesses Drive via ADC under its own identity. NO Domain-Wide
# Delegation; ADR 0027's allowlist (`gmail.compose`, `calendar.readonly`)
# is unchanged. `dwd_scopes.md` and `drafts_boundary_check.py` audits
# remain clean.
#
# Image lifecycle mirrors triage_bridge: TF declares the resource at the
# `bootstrap` tag, manual rebuilds via cloudbuild.notes-ingestor.yaml
# swap tags post-merge. `lifecycle.ignore_changes = [image]` prevents
# TF from fighting that.

variable "notes_ingestor_image_tag" {
  description = "Container tag for asb-notes-ingestor. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.notes-ingestor.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "notes_ingestor_schedule" {
  description = "Cloud Scheduler cron for the notes ingestor. Default '0 13 * * *' (daily 13:00 UTC = 6am Pacific). Originally weekly per ADR 0031 §5; bumped to daily so voice memos recorded any day surface in that night's Evening Reflection (RecentVoiceMemosReader uses a 24h rolling window, so weekly cadence dropped Tue–Sun memos). Resource name retained as `tb_notes_ingestor_weekly` to avoid an unnecessary scheduler recreate."
  type        = string
  default     = "0 13 * * *"
}

variable "notes_default_folder_id" {
  description = "Drive folder id of 'Brain Inbox/Notes/' (default, non-HIPAA). Empty until the user has created the folder + shared it with asb-notes-ingestor-sa; the runbook (docs/runbooks/notes-ingestor.md) walks through capture."
  type        = string
  default     = ""
}

variable "notes_hipaa_folder_id" {
  description = "Drive folder id of 'Brain/Inbox/HIPAA/' (HIPAA-cascade ON). Optional — empty disables HIPAA-folder ingestion. Notes from this folder publish to triage with the hipaa_excluded aspect, which short-circuits at BaseAgent's HIPAA guard (ADR 0006). Repurposed in ADR 0037 from the legacy 'Brain Inbox/Notes-HIPAA/' path."
  type        = string
  default     = ""
}

# ADR 0037 §2 — IPARAG-adapted Brain/ folder layout. All six are
# optional; unset env vars silently skip in the ingestor's folder loop.
# This supports gradual rollout where the user creates folders and
# fills in IDs over time.

variable "brain_inbox_voice_folder_id" {
  description = "Drive folder id of 'Brain/Inbox/Voice/' (ADR 0037 §2). Voice memos: .m4a/.mp3/.wav. Files transcribe via Gemini multimodal audio prompt. INBOX kind → publishes to triage."
  type        = string
  default     = ""
}

variable "brain_inbox_quicknotes_folder_id" {
  description = "Drive folder id of 'Brain/Inbox/QuickNotes/' (ADR 0037 §2). Short markdown captures. INBOX kind → publishes to triage."
  type        = string
  default     = ""
}

variable "brain_inbox_reading_folder_id" {
  description = "Drive folder id of 'Brain/Inbox/Reading/' (ADR 0037 §2). Saved articles, web clippings, PDFs to read. INBOX kind → publishes to triage."
  type        = string
  default     = ""
}

variable "brain_areas_folder_id" {
  description = "Drive folder id of 'Brain/Areas/' (ADR 0037 §2). Ongoing reference material (responsibilities, profiles). AREA kind — does NOT publish to triage; semantically searchable via embeddings."
  type        = string
  default     = ""
}

variable "brain_resources_folder_id" {
  description = "Drive folder id of 'Brain/Resources/' (ADR 0037 §2). Static templates, frameworks, reusables. RESOURCE kind — does NOT publish to triage; semantically searchable via embeddings."
  type        = string
  default     = ""
}

variable "brain_archives_folder_id" {
  description = "Drive folder id of 'Brain/Archives/' (ADR 0037 §2). Cold storage of completed projects, deprecated reference material. ARCHIVE kind — does NOT publish to triage; semantically searchable via embeddings."
  type        = string
  default     = ""
}

# ADR 0048 §2-3 — the agency Shared Drive sweep. The clients
# root is the discovery target (per-client HIPAA filter + allowlisted
# subfolders); the four internal roots are walked recursively without
# filtering. All five are optional — unset env vars silently skip.
# `asb-notes-ingestor-sa` must be added as a Viewer on the Agency
# Solutions Shared Drive separately (manual step; see runbook
# `docs/runbooks/notes-ingestor.md` §Solutions Drive access).

variable "solutions_clients_folder_id" {
  description = "Drive folder id of the agency/05_CLIENTS/ (ADR 0048 §3). Empty disables the per-client sweep. When set, the ingester walks immediate children, skips 00_CLIENT_TEMPLATE and HIPAA-flagged clients (matched by normalized folder name against airtable_replica.accounts.hipaa), and for each remaining client walks the allowlisted subfolders 00_ONBOARDING / 01_STRATEGY / 05_CAMPAIGNS_AND_CHANNELS / 07_REPORTING (EXTERNAL) / 08_MEETING_NOTES recursively."
  type        = string
  default     = ""
}

variable "solutions_management_legal_folder_id" {
  description = "Drive folder id of the agency/01_MANAGEMENT & LEGAL/ (ADR 0048 §2). Recursive sweep, AGENCY scope. Optional."
  type        = string
  default     = ""
}

variable "solutions_finance_folder_id" {
  description = "Drive folder id of the agency/02_FINANCE & ACCOUNTING/ (ADR 0048 §2). Recursive sweep, AGENCY scope. Optional."
  type        = string
  default     = ""
}

variable "solutions_operations_hr_folder_id" {
  description = "Drive folder id of the agency/03_OPERATIONS & HR/ (ADR 0048 §2). Recursive sweep, AGENCY scope. Optional."
  type        = string
  default     = ""
}

variable "solutions_sales_marketing_folder_id" {
  description = "Drive folder id of the agency/04_SALES & MARKETING (Internal)/ (ADR 0048 §2). Recursive sweep, AGENCY scope. Optional."
  type        = string
  default     = ""
}

variable "brain_embedding_model" {
  description = "Vertex embedding model for the notes ingestor (ADR 0038 §1). Default 'text-embedding-005' (768-dim). Setting via UPDATE notes SET embedding_model = NULL triggers re-embed via the idempotency guard (ADR 0038 §4)."
  type        = string
  default     = "text-embedding-005"
}

variable "notes_ingestor_max_per_tick" {
  description = "Cap on files processed per scheduler tick (ADR 0031 §6). Bounds Vertex spend if the user bulk-shares a backlog. Sized for the weekly cadence — assumes a 2-person tool capturing < 100 notes/week."
  type        = string
  default     = "100"
}

variable "notes_ingestor_backfill_mode" {
  description = "ADR 0039 §4 — embeddings backfill switch. Default 'none' runs the normal folder loop. Setting 'embeddings_only' makes the Job skip the folder loop and instead embed pre-ADR-0038 rows in agent_outputs.notes (manual one-shot — ``gcloud run jobs execute --update-env-vars=BACKFILL_MODE=embeddings_only``). Once the corpus is fully embedded, revert to 'none'."
  type        = string
  default     = "none"
}

variable "notes_ingestor_max_backfill_per_tick" {
  description = "ADR 0039 §4 — cap on rows the embeddings backfill processes per Job execution. Bounds Vertex spend on first-run."
  type        = string
  default     = "100"
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------
# Job SA: asb-notes-ingestor-sa. Scoped to BQ writes on agent_outputs.notes
# + agent_audit_log + agent_state.notes_ingestor_watermark, Pub/Sub publish
# on asb-triage-input, Vertex predict, and Drive (folder-level shared by
# the user — no API IAM for folder access; the folder share IS the grant).
#
# Invoker SA: asb-notes-ingestor-invoker — holds run.invoker only; Cloud
# Scheduler authenticates as this SA via OIDC.

resource "google_service_account" "tb_notes_ingestor_sa" {
  project      = var.brain_project_id
  account_id   = "asb-notes-ingestor-sa"
  display_name = "Agency Notes Ingestor"
  description  = "Runs the WS-G Samsung Notes ingestor (ADR 0031). Polls two Drive folders shared with this SA email by the user; writes agent_outputs.notes; publishes to asb-triage-input. No DWD; folder access via folder-level share."
}

resource "google_service_account" "tb_notes_ingestor_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-notes-ingestor-invoker"
  display_name = "Cloud Scheduler invoker for asb-notes-ingestor"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_notes_ingestor" {
  project     = var.brain_project_id
  role_id     = "tbNotesIngestor"
  title       = "Agency Notes Ingestor"
  description = "Project-level permissions for the WS-G notes ingestor. BQ dataset access is granted separately via google_bigquery_dataset_iam_member. ADR 0031."
  stage       = "GA"
  permissions = [
    # BigQuery query exec (data access scoped via dataset IAM below).
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    # Pub/Sub publish into asb-triage-input — no consumer permissions.
    "pubsub.topics.publish",
    "pubsub.topics.get",
    # Vertex AI generate_content for Gemini multimodal extraction.
    "aiplatform.endpoints.predict",
    # Agent Observability traces.
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_notes_ingestor_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_notes_ingestor.id
  member  = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# Write notes rows + read agent_outputs.notes for dedup pre-check.
resource "google_bigquery_dataset_iam_member" "tb_notes_ingestor_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# Write per-invocation audit rows (ADR 0006 BaseAgent contract; the
# ingestor emits directly because it isn't a BaseAgent subclass).
resource "google_bigquery_dataset_iam_member" "tb_notes_ingestor_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# Write watermark advances + read most-recent watermark per folder.
resource "google_bigquery_dataset_iam_member" "tb_notes_ingestor_state_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_state.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# ADR 0048 §3 — read airtable_replica.accounts.hipaa to filter HIPAA-flagged
# client folders out of the Solutions sweep at discovery time (defense in
# depth: HIPAA content is never even listed, let alone embedded). The
# airtable_replica dataset is owned by the data_pipeline module; referenced
# here by literal dataset id so this binding doesn't fight TF dependency
# ordering across modules.
resource "google_bigquery_dataset_iam_member" "tb_notes_ingestor_airtable_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# Pub/Sub publisher on asb-triage-input. The topic itself is owned by the
# data_pipeline module (it was created with WS-B for the inbound signal
# fan-in) and referenced from agent_runtime via its literal name. This
# binding adds the new SA as a publisher without touching any existing
# bindings.
resource "google_pubsub_topic_iam_member" "tb_notes_ingestor_publisher" {
  project = var.brain_project_id
  topic   = "asb-triage-input"
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.tb_notes_ingestor_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  notes_ingestor_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/notes-ingestor:${var.notes_ingestor_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_notes_ingestor" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-notes-ingestor"

  # Cloud Run Jobs v2 defaults this to true, which blocks destroy+create
  # when the resource is tainted (e.g., after a failed first-deploy with
  # a missing image). The Job has no persistent state — execution
  # history lives in agent_audit_log.events; recreating is cheap.
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_notes_ingestor_sa.email
      timeout         = "1800s" # weekly cadence drains a week's accumulated notes; bumped from 540s to give multi-note backlogs headroom
      max_retries     = 0       # per-file errors land in the audit log; next tick retries via watermark

      containers {
        image = local.notes_ingestor_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "NOTES_FOLDER_ID"
          value = var.notes_default_folder_id
        }
        env {
          name  = "NOTES_HIPAA_FOLDER_ID"
          value = var.notes_hipaa_folder_id
        }
        # ADR 0037 §2 — Brain/ folder lanes. Empty string = unset =
        # silently skipped by the ingestor's folder loop.
        env {
          name  = "BRAIN_INBOX_VOICE_FOLDER_ID"
          value = var.brain_inbox_voice_folder_id
        }
        env {
          name  = "BRAIN_INBOX_QUICKNOTES_FOLDER_ID"
          value = var.brain_inbox_quicknotes_folder_id
        }
        env {
          name  = "BRAIN_INBOX_READING_FOLDER_ID"
          value = var.brain_inbox_reading_folder_id
        }
        env {
          name  = "BRAIN_AREAS_FOLDER_ID"
          value = var.brain_areas_folder_id
        }
        env {
          name  = "BRAIN_RESOURCES_FOLDER_ID"
          value = var.brain_resources_folder_id
        }
        env {
          name  = "BRAIN_ARCHIVES_FOLDER_ID"
          value = var.brain_archives_folder_id
        }
        # ADR 0048 §2-3 — the agency Shared Drive sweep. Empty
        # string = unset = silently skipped by main._build_solutions_folders.
        env {
          name  = "SOLUTIONS_CLIENTS_FOLDER_ID"
          value = var.solutions_clients_folder_id
        }
        env {
          name  = "SOLUTIONS_MANAGEMENT_LEGAL_FOLDER_ID"
          value = var.solutions_management_legal_folder_id
        }
        env {
          name  = "SOLUTIONS_FINANCE_FOLDER_ID"
          value = var.solutions_finance_folder_id
        }
        env {
          name  = "SOLUTIONS_OPERATIONS_HR_FOLDER_ID"
          value = var.solutions_operations_hr_folder_id
        }
        env {
          name  = "SOLUTIONS_SALES_MARKETING_FOLDER_ID"
          value = var.solutions_sales_marketing_folder_id
        }
        env {
          name  = "EMBEDDING_MODEL"
          value = var.brain_embedding_model
        }
        env {
          name  = "NOTES_INGESTOR_SA_EMAIL"
          value = google_service_account.tb_notes_ingestor_sa.email
        }
        env {
          name  = "MAX_NOTES_PER_TICK"
          value = var.notes_ingestor_max_per_tick
        }
        env {
          name  = "VERTEX_LOCATION"
          value = var.region
        }
        # ADR 0039 §4 — embeddings backfill switch. Default 'none' is a
        # no-op (folder loop runs as ADR 0031 / 0037). Set to
        # 'embeddings_only' via `gcloud run jobs execute --update-env-vars`
        # for a one-shot backfill of pre-ADR-0038 rows; revert after.
        env {
          name  = "BACKFILL_MODE"
          value = var.notes_ingestor_backfill_mode
        }
        env {
          name  = "MAX_BACKFILL_PER_TICK"
          value = var.notes_ingestor_max_backfill_per_tick
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi" # Vertex multimodal carries the PDF body in-memory; 512Mi was tight for >10-page notes
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.tb_notes_ingestor_role,
    google_bigquery_dataset_iam_member.tb_notes_ingestor_outputs_editor,
    google_bigquery_dataset_iam_member.tb_notes_ingestor_audit_writer,
    google_bigquery_dataset_iam_member.tb_notes_ingestor_state_editor,
    google_bigquery_dataset_iam_member.tb_notes_ingestor_airtable_replica_viewer,
    google_pubsub_topic_iam_member.tb_notes_ingestor_publisher,
    google_bigquery_table.notes,
    google_bigquery_table.notes_ingestor_watermark,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "notes_ingestor_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_notes_ingestor.project
  location = google_cloud_run_v2_job.tb_notes_ingestor.location
  name     = google_cloud_run_v2_job.tb_notes_ingestor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_notes_ingestor_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily (was weekly; see notes_ingestor_schedule comment)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_notes_ingestor_weekly" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-notes-ingestor-weekly"
  schedule  = var.notes_ingestor_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the Notes Ingestor daily (originally weekly per ADR 0031 §5; bumped to daily so voice memos surface in tonight's Evening Reflection — see Phase A in the daily-reflection-doc plan). Polls the Brain Inbox Drive folders shared with asb-notes-ingestor-sa; one tick = one Cloud Run Job execution. 13:00 UTC = 6am Pacific so notes are ingested before the 7:25am morning brief tick. Resource + scheduler name retained as 'weekly' to avoid an unnecessary recreate."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_notes_ingestor.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_notes_ingestor_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.notes_ingestor_scheduler_invoker]
}
