# ADR 0039 — Captures materializer: Cloud Run Job + 15-min Cloud Scheduler.
#
# Reads unsynced rows from `airtable_replica.captures`, dispatches each by
# Kind (note/decision/win/todo) into agent_outputs.* or asb-triage-input,
# flips Synced=TRUE in Airtable, then DELETEs the source row.
#
# Topology mirrors notes-ingestor (ADR 0031 / 0037) — Cloud Run Job + Cloud
# Scheduler + dedicated invoker SA. The Job runs as `asb-captures-materializer-sa`
# (separate from asb-notes-ingestor-sa per ADR 0039 §1: the materializer
# needs Airtable WRITE access via the airtable-tasks-write-pat-prod Secret;
# notes-ingestor doesn't, and folding it in would broaden the ingestor's
# blast radius).
#
# Image lifecycle mirrors triage_bridge / notes-ingestor: TF declares the
# resource at the `bootstrap` tag, manual rebuilds via
# cloudbuild.captures-materializer.yaml swap tags post-merge.
# `lifecycle.ignore_changes = [image]` prevents TF from fighting that.

variable "captures_materializer_image_tag" {
  description = "Container tag for asb-captures-materializer (ADR 0039). 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.captures-materializer.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "captures_materializer_schedule" {
  description = "Cloud Scheduler cron for the materializer. Default '*/15 * * * *' matches asb-airtable-sync-15m so a form submission lands in BQ within 15-30 min worst case."
  type        = string
  default     = "*/15 * * * *"
}

variable "captures_materializer_max_per_tick" {
  description = "Cap on captures processed per scheduler tick (ADR 0039 §1). Bounds inline-embed Vertex spend if a backlog accumulates."
  type        = string
  default     = "100"
}

# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------
# Job SA: asb-captures-materializer-sa. Scoped to BQ read on
# airtable_replica, BQ write on agent_outputs + agent_audit_log, Pub/Sub
# publish on asb-triage-input, Vertex predict (inline embed for `note`
# Kind per ADR 0039 §6), and Secret Manager accessor on
# airtable-tasks-write-pat-prod.
#
# Invoker SA: asb-captures-materializer-invoker — holds run.invoker only;
# Cloud Scheduler authenticates as this SA via OIDC.

resource "google_service_account" "tb_captures_materializer_sa" {
  project      = var.brain_project_id
  account_id   = "asb-captures-materializer-sa"
  display_name = "Agency Captures Materializer"
  description  = "Runs the WS-G PKM Phase 0b Captures materializer (ADR 0039). Reads airtable_replica.captures; writes agent_outputs.{notes,decisions,wins}; mutates Airtable Captures rows via airtable-tasks-write-pat-prod. No DWD."
}

resource "google_service_account" "tb_captures_materializer_invoker_sa" {
  project = var.brain_project_id
  # account_id is capped at 30 chars; shortened from -materializer-invoker.
  account_id   = "asb-captures-mat-invoker"
  display_name = "Cloud Scheduler invoker for asb-captures-materializer"
  description  = "Holds run.invoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC."
}

# ---------------------------------------------------------------------------
# Custom IAM role + bindings
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_captures_materializer" {
  project     = var.brain_project_id
  role_id     = "tbCapturesMaterializer"
  title       = "Agency Captures Materializer"
  description = "Project-level permissions for the WS-G PKM Phase 0b Captures materializer. BQ dataset access is granted separately via google_bigquery_dataset_iam_member. ADR 0039."
  stage       = "GA"
  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
    "pubsub.topics.publish",
    "pubsub.topics.get",
    # ADR 0039 §6 — `note` Kind dispatch embeds inline, so the SA needs
    # Vertex predict. The other Kinds don't, but a single role is simpler
    # than splitting per-Kind.
    "aiplatform.endpoints.predict",
    "cloudtrace.traces.patch",
  ]
}

resource "google_project_iam_member" "tb_captures_materializer_role" {
  project = var.brain_project_id
  role    = google_project_iam_custom_role.tb_captures_materializer.id
  member  = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# Read airtable_replica.captures (the unsynced row scan).
resource "google_bigquery_dataset_iam_member" "tb_captures_materializer_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# Write agent_outputs.{notes,decisions,wins} + dedup pre-check SELECTs.
resource "google_bigquery_dataset_iam_member" "tb_captures_materializer_outputs_editor" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_outputs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# Per-invocation audit row (ADR 0006 BaseAgent contract; the materializer
# emits directly because it isn't a BaseAgent subclass — same shape as
# notes-ingestor).
resource "google_bigquery_dataset_iam_member" "tb_captures_materializer_audit_writer" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.agent_audit_log.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# Pub/Sub publisher on asb-triage-input. Topic owned by the data_pipeline
# module; this binding adds the new SA without touching existing ones.
resource "google_pubsub_topic_iam_member" "tb_captures_materializer_publisher" {
  project = var.brain_project_id
  topic   = "asb-triage-input"
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# Read airtable-tasks-write-pat-prod Secret. Same PAT the Triage Agent's
# TaskDrafter uses (ADR 0039 §Consequences). The PAT was issued with
# `data.records:write` on the Operations base, so it covers Captures.
resource "google_secret_manager_secret_iam_member" "tb_captures_materializer_pat_accessor" {
  project   = var.brain_project_id
  secret_id = "airtable-tasks-write-pat-prod"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_captures_materializer_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  captures_materializer_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/captures-materializer:${var.captures_materializer_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_captures_materializer" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-captures-materializer"

  # Mirrors triage_bridge / notes-ingestor / morning-brief: deletion_protection
  # off so a tainted resource (e.g. failed first-deploy with a missing image)
  # can be recreated. Job has no persistent state — execution history lives
  # in agent_audit_log.events.
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_captures_materializer_sa.email
      timeout         = "300s"
      max_retries     = 0

      containers {
        image = local.captures_materializer_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "AIRTABLE_BASE_ID"
          value = var.airtable_base_id
        }
        env {
          name  = "AIRTABLE_TASKS_WRITE_PAT_SECRET"
          value = "airtable-tasks-write-pat-prod"
        }
        env {
          name  = "CAPTURES_MATERIALIZER_SA_EMAIL"
          value = google_service_account.tb_captures_materializer_sa.email
        }
        env {
          name  = "MAX_CAPTURES_PER_TICK"
          value = var.captures_materializer_max_per_tick
        }
        env {
          name  = "VERTEX_LOCATION"
          value = var.region
        }
        env {
          name  = "EMBEDDING_MODEL"
          value = var.brain_embedding_model
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
    google_project_iam_member.tb_captures_materializer_role,
    google_bigquery_dataset_iam_member.tb_captures_materializer_replica_viewer,
    google_bigquery_dataset_iam_member.tb_captures_materializer_outputs_editor,
    google_bigquery_dataset_iam_member.tb_captures_materializer_audit_writer,
    google_pubsub_topic_iam_member.tb_captures_materializer_publisher,
    google_secret_manager_secret_iam_member.tb_captures_materializer_pat_accessor,
    google_bigquery_table.notes,
    google_bigquery_table.decisions,
    google_bigquery_table.wins,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "captures_materializer_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_captures_materializer.project
  location = google_cloud_run_v2_job.tb_captures_materializer.location
  name     = google_cloud_run_v2_job.tb_captures_materializer.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tb_captures_materializer_invoker_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — every 15 minutes (matches asb-airtable-sync-15m cadence)
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "tb_captures_materializer_15m" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-captures-materializer-15m"
  schedule  = var.captures_materializer_schedule
  time_zone = "Etc/UTC"

  description = "Triggers the Captures materializer every 15 min (ADR 0039 §1). Form submissions land in BQ within 15-30 min worst case (one airtable-sync cycle + one materializer tick)."

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_captures_materializer.name}:run"

    oauth_token {
      service_account_email = google_service_account.tb_captures_materializer_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.captures_materializer_scheduler_invoker]
}
