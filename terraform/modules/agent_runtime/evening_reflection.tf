# WS-G4 Evening Reflection — Cloud Run Job + daily Cloud Schedulers.
# ADR 0036: topology mirrors morning_brief.tf (ADR 0029) — Cloud Run Job
# running as the existing asb-agent-triage-sa (one SA / one delegation
# surface, per ADR 0027 §2). DWD scopes reused: gmail.compose +
# calendar.readonly. No new IAM bindings needed beyond the invoker SA;
# the Job's read/write surface is identical to morning_brief's plus the
# new agent_outputs.evening_reflections table (which inherits the
# dataset-level dataEditor binding asb-agent-triage-sa already has).
#
# ADR 0040 PR-C: two schedulers now invoke the same Job with different
# `REFLECTION_MODE` containerOverrides:
#   - asb-evening-reflection-daily : 21:00 PT, REFLECTION_MODE=reflect
#     (kept its original GCP name — destroy/create avoidance)
#   - asb-evening-prompt-daily     : 16:00 PT, REFLECTION_MODE=prompt
# Image rebuild for PR-C ships at tag adr-0040-evening-reflection-v1
# (built post-merge via cloudbuild.evening-reflection.yaml).
#
# Image lifecycle mirrors morning_brief: TF declares the resource at the
# `bootstrap` tag, manual rebuilds via cloudbuild.evening-reflection.yaml
# swap tags post-merge. `lifecycle.ignore_changes = [image]` prevents
# TF from fighting that.

variable "evening_reflection_image_tag" {
  description = "Container tag for asb-evening-reflection. 'bootstrap' is a placeholder so the first terraform apply succeeds before cloudbuild.evening-reflection.yaml has built and pushed a real one."
  type        = string
  default     = "bootstrap"
}

variable "evening_reflection_schedule" {
  description = "Cloud Scheduler cron expression for the daily REFLECT-mode tick. ADR 0040 PR-C shifted from 0 18 → 0 21 (9:00pm) so the reflect window covers the full workday including post-dinner voice memos."
  type        = string
  default     = "0 21 * * *"
}

variable "evening_prompt_schedule" {
  description = "Cloud Scheduler cron expression for the daily PROMPT-mode anchor (ADR 0040 §1). Default 0 16 * * * (4:00pm every day) interpreted in evening_reflection_timezone."
  type        = string
  default     = "0 16 * * *"
}

variable "evening_reflection_timezone" {
  description = "IANA tz for the evening_reflection_schedule cron. Cloud Scheduler handles DST."
  type        = string
  default     = "America/Los_Angeles"
}

variable "evening_reflection_recipients" {
  description = "Comma-separated list of Workspace user emails who receive the daily reflection. v1: just the operator."
  type        = string
  default     = "owner@example.com"
}

variable "brain_areas_reflections_folder_id" {
  description = "ADR 0044 — Drive folder id of 'Brain/Areas/Reflections/'. The user creates the folder and shares it with asb-agent-triage-sa as Editor before this is set; until populated, REFLECT mode falls back to the Gmail-draft surface (the v1 path stays online so the daily ritual never breaks during rollout)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service account — Cloud Scheduler invoker
# ---------------------------------------------------------------------------
# The Cloud Run Job itself runs as the existing asb-agent-triage-sa; only
# the OIDC invoker needs a fresh SA.

resource "google_service_account" "tb_evening_reflection_invoker_sa" {
  project      = var.brain_project_id
  account_id   = "asb-evening-reflection-invoker"
  display_name = "Cloud Scheduler invoker for asb-evening-reflection"
  description  = "Holds tbRunJobWithOverridesInvoker on the Cloud Run Job only. Cloud Scheduler authenticates as this SA via OIDC and POSTs with containerOverrides body (ADR 0040 PR-C), which requires run.jobs.runWithOverrides beyond what roles/run.invoker grants."
}

# ADR 0040 PR-C added a containerOverrides body to both evening-reflection
# schedulers (to dispatch REFLECTION_MODE per scheduler). Cloud Run's :run
# endpoint rejects POSTs carrying overrides unless the caller holds
# `run.jobs.runWithOverrides` — which roles/run.invoker does NOT include
# (it only grants run.jobs.run). Without this binding, every scheduler tick
# returns HTTP 403 PERMISSION_DENIED and no execution ever starts; the
# breakage is silent at the scheduler level and produces no BQ row.
#
# This custom role is the minimal pair of permissions needed; binding it
# at the Cloud Run Job resource level (not project) keeps the blast radius
# to that single Job. Pattern follows tbAgentTriage / tbRoutingFanout /
# tbRiskWatcher project-level custom roles, but with a smaller surface.
resource "google_project_iam_custom_role" "tb_run_job_with_overrides_invoker" {
  project     = var.brain_project_id
  role_id     = "tbRunJobWithOverridesInvoker"
  title       = "Agency Cloud Run Job Invoker (with overrides)"
  description = "Minimum permissions for a Cloud Scheduler OIDC SA that POSTs to a Cloud Run Job's :run endpoint with a containerOverrides body. roles/run.invoker is insufficient because it lacks run.jobs.runWithOverrides."
  stage       = "GA"
  permissions = [
    "run.jobs.run",
    "run.jobs.runWithOverrides",
  ]
}

# ---------------------------------------------------------------------------
# Cloud Run Job
# ---------------------------------------------------------------------------

locals {
  evening_reflection_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_agents.repository_id}/evening-reflection:${var.evening_reflection_image_tag}"
}

resource "google_cloud_run_v2_job" "tb_evening_reflection" {
  project  = var.brain_project_id
  location = var.region
  name     = "asb-evening-reflection"

  # Cloud Run Jobs v2 defaults this to true, which blocks destroy+create
  # when the resource is tainted (e.g., after a failed first-deploy with
  # a missing image). The Job has no persistent state — execution
  # history lives in agent_audit_log.events; recreating is cheap.
  # Mirrors the same flag on asb-morning-brief / asb-notes-ingestor.
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.tb_agent_triage_sa.email
      # ADR 0044 — bumped from 300s to 600s. REFLECT mode now does:
      # structured Gemini call + Drive Doc create + Chat post + future
      # VECTOR_SEARCH (Phase C). Cold start + first VECTOR_SEARCH adds
      # ~15s; the previous 300s ceiling was too tight.
      timeout     = "600s"
      max_retries = 0

      containers {
        image = local.evening_reflection_image

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "REFLECTION_RECIPIENTS"
          value = var.evening_reflection_recipients
        }
        env {
          name  = "REFLECTION_TIMEZONE"
          value = var.evening_reflection_timezone
        }
        env {
          name  = "TRIAGE_SA_EMAIL"
          value = google_service_account.tb_agent_triage_sa.email
        }
        env {
          name  = "BRAIN_AREAS_REFLECTIONS_FOLDER_ID"
          value = var.brain_areas_reflections_folder_id
        }
        # ADR 0044 — Chat-card notification with link to today's
        # Reflection Doc. Webhook URL materializes from Secret Manager
        # (`second-brain-gchat-webhook`) so the secret never lands in
        # plaintext TF / image. brag_spotter.tf / routing_chat_secret.tf
        # use the same accessor pattern.
        env {
          name = "BRAIN_ALERTS_CHAT_WEBHOOK_URL"
          value_source {
            secret_key_ref {
              secret  = "second-brain-gchat-webhook"
              version = "latest"
            }
          }
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
    google_bigquery_dataset_iam_member.tb_agent_triage_outputs_editor,
    google_bigquery_dataset_iam_member.tb_agent_triage_replica_viewer,
    google_bigquery_dataset_iam_member.tb_agent_triage_audit_writer,
    google_bigquery_table.evening_reflections,
    google_secret_manager_secret_iam_member.tb_evening_reflection_chat_secret_accessor,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "evening_reflection_scheduler_invoker" {
  project  = google_cloud_run_v2_job.tb_evening_reflection.project
  location = google_cloud_run_v2_job.tb_evening_reflection.location
  name     = google_cloud_run_v2_job.tb_evening_reflection.name
  role     = google_project_iam_custom_role.tb_run_job_with_overrides_invoker.id
  member   = "serviceAccount:${google_service_account.tb_evening_reflection_invoker_sa.email}"
}

# ADR 0044 — Reflection's daily Chat-card needs the Brain alerts webhook
# URL. asb-agent-triage-sa already runs the Job; this binding lets it
# read `second-brain-gchat-webhook` so the BRAIN_ALERTS_CHAT_WEBHOOK_URL
# secret_key_ref env var resolves at startup. Mirrors brag_spotter's
# `tb_brag_spotter_chat_secret_accessor` (brag_spotter.tf:147).
resource "google_secret_manager_secret_iam_member" "tb_evening_reflection_chat_secret_accessor" {
  project   = var.brain_project_id
  secret_id = "second-brain-gchat-webhook"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — daily evening trigger
# ---------------------------------------------------------------------------
# Lands paused so the first scheduled tick can't fire against a
# placeholder/bootstrap image. PR-B rollout: build first real image →
# update tfvars to the new tag → targeted apply → manual smoke fire via
# `gcloud run jobs execute` → unpause via
# `gcloud scheduler jobs resume asb-evening-reflection-daily`.

locals {
  # ADR 0040 PR-C: container override body that pins REFLECTION_MODE per
  # scheduler. The :run endpoint accepts overrides.containerOverrides;
  # google_cloud_scheduler_job.http_target.body wants base64-encoded
  # bytes. The container name is matched against the image's container
  # spec (Cloud Run v2 default container; we omit the "name" field to
  # apply to the single container).
  evening_reflect_override_body = base64encode(jsonencode({
    overrides = {
      containerOverrides = [{
        env = [{ name = "REFLECTION_MODE", value = "reflect" }]
      }]
    }
  }))

  evening_prompt_override_body = base64encode(jsonencode({
    overrides = {
      containerOverrides = [{
        env = [{ name = "REFLECTION_MODE", value = "prompt" }]
      }]
    }
  }))
}

# REFLECT-mode scheduler. GCP name kept as `asb-evening-reflection-daily`
# (ADR 0040 PR-C closeout addendum — cosmetic rename to
# `asb-evening-reflect-daily` deferred to avoid a destroy/create gap).
resource "google_cloud_scheduler_job" "tb_evening_reflection_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-evening-reflection-daily"
  schedule  = var.evening_reflection_schedule
  time_zone = var.evening_reflection_timezone

  description = "Daily REFLECT-mode kick-off for the Evening Reflection Cloud Run Job. ADRs 0036, 0040."

  # ADR 0059 — Evening Reflection REFLECT mode retired in favor of the
  # operator's scheduled Claude workflow. `ignore_changes = [paused]`
  # removed so TF actively enforces the pause (mirrors what ADR 0056 did
  # to asb-evening-prompt-daily). The Cloud Run Job stays deployed for a
  # one-line revert if the Claude workflow proves unreliable.
  paused = true

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_evening_reflection.name}:run"
    body        = local.evening_reflect_override_body

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.tb_evening_reflection_invoker_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.evening_reflection_scheduler_invoker]
}

# PROMPT-mode scheduler (ADR 0040 §1). New resource introduced in PR-C —
# fires the same Cloud Run Job with REFLECTION_MODE=prompt, which loads
# prompt_v1.md and skips structured extraction.
resource "google_cloud_scheduler_job" "tb_evening_prompt_daily" {
  project   = var.brain_project_id
  region    = var.region
  name      = "asb-evening-prompt-daily"
  schedule  = var.evening_prompt_schedule
  time_zone = var.evening_reflection_timezone

  description = "Daily PROMPT-mode kick-off for the Evening Reflection Cloud Run Job (4pm forward-looking anchor). ADR 0040 §1. Retired 2026-05-18 (ADR 0056) — Local Claude Code routine 'Daily evening reflection' is the canonical 6pm surface (richer personalization via brain_ask + open_risk_flags); Gmail-draft output no longer read. Job + SA + IAM stay deployed for one-line revert."

  paused = true

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.tb_evening_reflection.name}:run"
    body        = local.evening_prompt_override_body

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.tb_evening_reflection_invoker_sa.email
    }
  }

  # ADR 0056: ignore_changes = [paused] removed so TF actively enforces the
  # retired state. Was kept previously to permit manual gcloud-unpause after
  # smoke; that workflow doesn't apply to a retired scheduler.

  depends_on = [google_cloud_run_v2_job_iam_member.evening_reflection_scheduler_invoker]
}
