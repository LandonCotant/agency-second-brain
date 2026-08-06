# WS-F PR #2 — Runtime audit Cloud Run Jobs.
#
# Pattern: per-script SA + custom IAM role + invoker SA + Cloud Scheduler,
# mirroring terraform/modules/data_pipeline/airtable_sync.tf.
#
# Four jobs, each on its own cadence:
#  - asb-audit-sensitive-isolation     (hourly)   PRD §4.1 layer 5
#  - asb-audit-sensitive-iam-drift     (daily)    PRD §4.1 layer 1
#  - asb-audit-drafts-boundary     (nightly)  PRD §4.7
#  - asb-audit-bucket-iam-drift    (daily)    ADR 0005 compensating control
#
# All four jobs run from a single asb-audit container image; each Cloud Run
# Job overrides the container args to invoke a specific module. Image tag
# is "bootstrap" until cloudbuild pushes a real one (same pattern as
# airtable_sync.tf — lifecycle.ignore_changes on the image attribute).
#
# Alerts: each script emits one row to agent_audit_log.events plus a
# structured stdout JSON line. The HIPAA isolation alert (alerts_hipaa.tf
# in WS-E) already matches jsonPayload.event:"HIPAA_GUARD_TRIPPED" — a
# breach detected by asb-audit-sensitive-isolation trips the existing P0 alert
# without any new notification plumbing here. Lower-severity drift events
# land in agent_audit_log.events; explicit dashboards/alerts are deferred
# to PR #3.

# ---------------------------------------------------------------------------
# Job catalog
# ---------------------------------------------------------------------------
# Single source of truth for per-job knobs. Each entry is a Cloud Run Job +
# Cloud Scheduler trigger keyed by short name. Keys are also embedded in SA
# email addresses so changing one rebuilds the SA — keep them stable.

locals {
  # Per-job `paused` controls whether the Cloud Scheduler trigger fires.
  # Default false; set true to suppress scheduled runs without tearing down
  # the Job/SA/IAM (preserves ability to flip back on with one apply).
  audit_jobs = {
    hipaa_isolation = {
      module      = "agency_brain.audit.hipaa_isolation_check"
      schedule    = "0 * * * *" # hourly on the hour
      timeout_sec = "300s"
      description = "HIPAA isolation runtime check (PRD §4.1 layer 5)"
      # Dormant until HIPAA ingestion ships (ADR 0055). The audit was 100%
      # false-positive because airtable_replica.clients doesn't exist —
      # HIPAA ingestion is deferred and the Operations base uses `accounts`
      # (ADR 0020). Re-enable per the ADR 0055 checklist when ingestion is
      # actually wired up.
      paused = true
    }
    hipaa_iam_drift = {
      module      = "agency_brain.audit.hipaa_iam_drift"
      schedule    = "0 6 * * *" # 06:00 UTC daily
      timeout_sec = "180s"
      description = "Brain project IAM drift check (PRD §4.1 layer 1)"
      # Re-enabled 2026-06-10 (code-review follow-up). Baseline
      # `expected/brain_iam_baseline.json` refreshed from live policy: the
      # diff was 12 custom agent roles (each bound to its own single agent
      # SA) + 2 GCP-managed service agents (artifactregistry.writer on
      # cloud-build, cloudaicompanion service agent), with NO member-level
      # drift on pre-existing roles — i.e. exactly the intentional post-WS-G1
      # additions ADR 0058 cited. Closes the ADR 0058 re-enable checklist.
      paused = false
    }
    drafts_boundary = {
      module      = "agency_brain.audit.drafts_boundary_check"
      schedule    = "0 3 * * *" # 03:00 UTC nightly
      timeout_sec = "180s"
      description = "Forbidden role check on agent SAs (PRD §4.7)"
      paused      = false
    }
    bucket_iam_drift = {
      module      = "agency_brain.audit.bucket_iam_drift"
      schedule    = "0 4 * * *" # 04:00 UTC daily
      timeout_sec = "180s"
      description = "GCS bucket IAM drift (ADR 0005 compensating control)"
      paused      = false
    }
    cost_daily_check = {
      module      = "agency_brain.audit.daily_spend_check"
      schedule    = "0 9 * * *" # 09:00 UTC daily
      timeout_sec = "180s"
      description = "Daily spend check (ADR 0030)"
      paused      = false
      extra_env = {
        BILLING_EXPORT_DATASET = var.billing_export_dataset
        COST_THRESHOLDS_USD    = jsonencode(var.cost_thresholds_usd)
        CHAT_WEBHOOK_SECRET_ID = var.brain_alerts_chat_webhook_secret_id
      }
    }
    # W3 hardening (2026-05-28 audit). Nightly run of
    # scripts/check_airtable_schema_drift.py's pure-logic via the
    # agency_brain.audit.airtable_schema_drift entrypoint. Catches the
    # Phase 0 bug class (schema.json says REQUIRED, live row blank) before
    # it crashes the next sync run, and surfaces structural drift
    # (new fields, type changes) as agent_audit_log.events rows that the
    # W5 fleet dashboard reads.
    airtable_schema_drift = {
      module      = "agency_brain.audit.airtable_schema_drift"
      schedule    = "0 2 * * *" # 02:00 UTC daily
      timeout_sec = "180s"
      description = "Airtable schema drift check (W3 audit hardening)"
      paused      = false
      extra_env = {
        AIRTABLE_BASE_ID       = var.airtable_base_id
        AIRTABLE_PAT_SECRET_ID = var.airtable_pat_secret_id
      }
    }
  }

  # SA short names follow asb-audit-<key>-sa to keep the email-derived
  # principal string under the 30-char account_id limit.
  audit_sa_account_ids = {
    hipaa_isolation       = "asb-audit-sensitive-iso"
    hipaa_iam_drift       = "asb-audit-sensitive-iam"
    drafts_boundary       = "asb-audit-drafts-bnd"
    bucket_iam_drift      = "asb-audit-bucket-iam"
    cost_daily_check      = "asb-audit-cost-daily"
    airtable_schema_drift = "asb-audit-airtbl-drift"
  }
}

# ---------------------------------------------------------------------------
# API enablement
# ---------------------------------------------------------------------------
# foundation enables iam + secretmanager; data_pipeline enables run +
# cloudscheduler + artifactregistry. WS-F adds cloudresourcemanager (for
# the IAM-policy reads) and storage (for the bucket IAM reads) — both are
# usually pre-enabled by foundation but declare explicitly so the module
# is self-contained.

locals {
  security_apis = [
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
}

resource "google_project_service" "security" {
  for_each = toset(local.security_apis)

  project            = var.brain_project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Per-script service accounts
# ---------------------------------------------------------------------------
# One SA per script. Per PRD §4.2 ("No shared service accounts. Each agent,
# sync flow, and routing flow gets its own SA with a custom IAM role
# containing only the permissions it needs.") the four checks have
# genuinely disjoint permission needs — consolidating to one SA would force
# the broadest-permissions union, which is exactly what we want to avoid.

resource "google_service_account" "audit" {
  for_each = local.audit_jobs

  project      = var.brain_project_id
  account_id   = local.audit_sa_account_ids[each.key]
  display_name = "Runtime audit — ${each.key}"
  description  = each.value.description
}

# Single invoker SA shared across all four schedulers. Cloud Scheduler
# binds run.invoker to this principal scoped to each Cloud Run Job — a
# compromise of this SA only grants invoke on these four specific jobs,
# nothing else.
resource "google_service_account" "runtime_audit_invoker" {
  project      = var.brain_project_id
  account_id   = "asb-runtime-audit-inv"
  display_name = "Cloud Scheduler invoker for asb-audit-* Cloud Run Jobs"
  description  = "Holds run.invoker on the four asb-audit-* jobs only. PRD §4.2."
}

# ---------------------------------------------------------------------------
# Custom IAM roles — minimum permissions per script
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "tb_audit_hipaa_isolation" {
  project     = var.brain_project_id
  role_id     = "tbAuditHipaaIsolation"
  title       = "Agency audit — HIPAA isolation"
  description = "BigQuery jobs.create + dataset metadata reads. PRD §4.2."
  stage       = "GA"

  permissions = [
    "bigquery.jobs.create",
    "bigquery.datasets.get",
  ]
}

resource "google_project_iam_custom_role" "tb_audit_hipaa_iam_drift" {
  project     = var.brain_project_id
  role_id     = "tbAuditHipaaIamDrift"
  title       = "Agency audit — Brain project IAM drift"
  description = "Cloud Resource Manager getIamPolicy on the brain project only."
  stage       = "GA"

  permissions = [
    "resourcemanager.projects.getIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "tb_audit_drafts_boundary" {
  project     = var.brain_project_id
  role_id     = "tbAuditDraftsBoundary"
  title       = "Agency audit — drafts boundary"
  description = "Same surface as IAM drift; checks forbidden roles bound to SAs."
  stage       = "GA"

  permissions = [
    "resourcemanager.projects.getIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "tb_audit_bucket_iam_drift" {
  project     = var.brain_project_id
  role_id     = "tbAuditBucketIamDrift"
  title       = "Agency audit — bucket IAM drift"
  description = "List buckets + read each bucket's IAM. ADR 0005 compensating control."
  stage       = "GA"

  permissions = [
    "storage.buckets.list",
    "storage.buckets.getIamPolicy",
  ]
}

resource "google_project_iam_custom_role" "tb_audit_cost_daily_check" {
  project     = var.brain_project_id
  role_id     = "tbAuditCostDailyCheck"
  title       = "Agency audit — daily spend check"
  description = "BigQuery jobs.create. dataViewer on billing_export is bound at dataset scope. ADR 0030."
  stage       = "GA"

  permissions = [
    "bigquery.jobs.create",
  ]
}

# W3 hardening (2026-05-28). The schema-drift job hits the Airtable Meta
# API + filterByFormula over plain HTTP. Project-scope BQ jobs.create is
# needed because AuditLogClient.emit() opens a BQ insert job (same as
# cost_daily_check). secretmanager.versions.access on the Airtable PAT is
# granted resource-scoped below.
resource "google_project_iam_custom_role" "tb_audit_airtable_schema_drift" {
  project     = var.brain_project_id
  role_id     = "tbAuditAirtableSchemaDrift"
  title       = "Agency audit — Airtable schema drift"
  description = "BigQuery jobs.create only. dataEditor on agent_audit_log is bound at dataset scope. Airtable PAT access is bound at the secret. W3 hardening 2026-05-28."
  stage       = "GA"

  permissions = [
    "bigquery.jobs.create",
  ]
}

# Map the custom roles to their corresponding jobs so the bindings below can
# be expressed as a single for_each loop.
locals {
  audit_custom_role_ids = {
    hipaa_isolation       = google_project_iam_custom_role.tb_audit_hipaa_isolation.id
    hipaa_iam_drift       = google_project_iam_custom_role.tb_audit_hipaa_iam_drift.id
    drafts_boundary       = google_project_iam_custom_role.tb_audit_drafts_boundary.id
    bucket_iam_drift      = google_project_iam_custom_role.tb_audit_bucket_iam_drift.id
    cost_daily_check      = google_project_iam_custom_role.tb_audit_cost_daily_check.id
    airtable_schema_drift = google_project_iam_custom_role.tb_audit_airtable_schema_drift.id
  }
}

resource "google_project_iam_member" "audit_custom_role" {
  for_each = local.audit_jobs

  project = var.brain_project_id
  role    = local.audit_custom_role_ids[each.key]
  member  = "serviceAccount:${google_service_account.audit[each.key].email}"
}

# ---------------------------------------------------------------------------
# Resource-scoped bindings
# ---------------------------------------------------------------------------
# All four scripts write a row to agent_audit_log.events on every run.
# Dataset-level dataEditor mirrors the airtable_sync precedent
# (airtable_sync.tf:88-93): a custom dataset role would be more precise but
# adds Terraform complexity that ADR 0005's pragmatic-security trade-off
# doesn't justify for a 2-person tool.

resource "google_bigquery_dataset_iam_member" "audit_log_writer" {
  for_each = local.audit_jobs

  project    = var.brain_project_id
  dataset_id = "agent_audit_log"
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.audit[each.key].email}"
}

# hipaa_isolation needs read on airtable_replica + agent_outputs to run
# its joins. dataViewer on the dataset is appropriate — the check reads
# every table, and dataset-level binding scales when new tables are added
# without re-touching this file.
resource "google_bigquery_dataset_iam_member" "hipaa_isolation_replica_viewer" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.audit["hipaa_isolation"].email}"
}

resource "google_bigquery_dataset_iam_member" "hipaa_isolation_outputs_viewer" {
  project    = var.brain_project_id
  dataset_id = "agent_outputs"
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.audit["hipaa_isolation"].email}"
}

# cost_daily_check needs read on the billing_export dataset (created out-of-band
# 2026-05-02 — see ADR 0030). dataViewer at dataset scope so new
# gcp_billing_export_v1_* shards are picked up automatically as the export rotates.
resource "google_bigquery_dataset_iam_member" "cost_daily_check_billing_viewer" {
  project    = var.brain_project_id
  dataset_id = var.billing_export_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.audit["cost_daily_check"].email}"
}

# cost_daily_check posts a daily summary card to the Brain alerts Chat space
# via the existing webhook secret (same one routing fan-out uses). secretAccessor
# on the secret object only — secret itself is operator-managed (mirrors
# agent_runtime/routing_chat_secret.tf).
resource "google_secret_manager_secret_iam_member" "cost_daily_check_chat_accessor" {
  count = var.brain_alerts_chat_webhook_secret_id != "" ? 1 : 0

  project   = var.brain_project_id
  secret_id = var.brain_alerts_chat_webhook_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audit["cost_daily_check"].email}"
}

# W3 hardening (2026-05-28). The schema-drift audit Job reads the same
# read-only Airtable PAT that data_pipeline/airtable_sync.tf consumes,
# scoped via a separate IAM binding so the audit SA only sees the one
# secret it actually needs.
resource "google_secret_manager_secret_iam_member" "airtable_schema_drift_pat_accessor" {
  count = var.airtable_pat_secret_id != "" ? 1 : 0

  project   = var.brain_project_id
  secret_id = var.airtable_pat_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audit["airtable_schema_drift"].email}"
}

# ---------------------------------------------------------------------------
# Artifact Registry repo for the asb-audit container image
# ---------------------------------------------------------------------------
# Separate repo from asb-sync (data_pipeline) so security workloads can
# adopt their own retention / cleanup policies later without touching the
# sync repo. Same naming pattern as data_pipeline.

resource "google_artifact_registry_repository" "tb_audit" {
  project       = var.brain_project_id
  location      = var.region
  repository_id = "asb-audit"
  format        = "DOCKER"
  description   = "Container images for the runtime audit Cloud Run Jobs (WS-F)."

  labels = {
    workstream = "ws-f"
  }

  # ADR 0024: keep the 5 most recent versions per tag and delete anything
  # older than 90 days. Both rules apply together; the live `:bootstrap`
  # tag is always one of the 5 most recent so it's never deleted.
  cleanup_policies {
    id     = "keep-recent-5"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "7776000s" # 90 days
    }
  }

  depends_on = [google_project_service.security]
}

locals {
  audit_image = "${var.region}-docker.pkg.dev/${var.brain_project_id}/${google_artifact_registry_repository.tb_audit.repository_id}/asb-audit:bootstrap"
}

# ---------------------------------------------------------------------------
# Cloud Run Jobs
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "audit" {
  for_each = local.audit_jobs

  project  = var.brain_project_id
  location = var.region
  name     = "asb-audit-${replace(each.key, "_", "-")}"

  template {
    template {
      service_account = google_service_account.audit[each.key].email
      timeout         = each.value.timeout_sec
      # Cloud Run Jobs default to 3 retries on failure. For an audit script,
      # a transient failure (BQ throttling, network blip) is fine to retry on
      # the next scheduler tick — re-trying immediately just multiplies the
      # cost of a real, persistent failure (which we want to surface fast in
      # the audit log, not bury under retry timeouts).
      max_retries = 0

      containers {
        image = local.audit_image
        # Override the image's ENTRYPOINT per-job so the four jobs can share
        # a single container build. `command` (not `args`) is required because
        # Dockerfile.audit deliberately leaves ENTRYPOINT unset — without a
        # `command` override the container has nothing to exec.
        command = ["python", "-m", each.value.module]

        env {
          name  = "BRAIN_PROJECT_ID"
          value = var.brain_project_id
        }
        env {
          name  = "AUDIT_SA_EMAIL"
          value = google_service_account.audit[each.key].email
        }
        # Per-job extra env (e.g., cost-check thresholds). Jobs that don't
        # set ``extra_env`` omit the field entirely; ``lookup`` short-circuits
        # to {} so the dynamic block emits nothing for them.
        dynamic "env" {
          for_each = lookup(each.value, "extra_env", {})
          content {
            name  = env.key
            value = env.value
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
    google_project_iam_member.audit_custom_role,
    google_bigquery_dataset_iam_member.audit_log_writer,
    google_bigquery_dataset_iam_member.hipaa_isolation_replica_viewer,
    google_bigquery_dataset_iam_member.hipaa_isolation_outputs_viewer,
    google_bigquery_dataset_iam_member.cost_daily_check_billing_viewer,
    google_secret_manager_secret_iam_member.cost_daily_check_chat_accessor,
    google_secret_manager_secret_iam_member.airtable_schema_drift_pat_accessor,
  ]

  # cloudbuild pushes a new image tag on every main merge; Terraform must
  # not fight that (mirrors airtable_sync.tf:186-190).
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# Cloud Scheduler invoker bindings
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job_iam_member" "audit_invoker" {
  for_each = local.audit_jobs

  project  = google_cloud_run_v2_job.audit[each.key].project
  location = google_cloud_run_v2_job.audit[each.key].location
  name     = google_cloud_run_v2_job.audit[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime_audit_invoker.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler — one trigger per job
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "audit" {
  for_each = local.audit_jobs

  project   = var.brain_project_id
  region    = var.region
  name      = "asb-audit-${replace(each.key, "_", "-")}-cron"
  schedule  = each.value.schedule
  time_zone = "Etc/UTC"
  paused    = each.value.paused

  description = each.value.description

  retry_config {
    retry_count          = 1
    max_retry_duration   = "60s"
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.brain_project_id}/jobs/${google_cloud_run_v2_job.audit[each.key].name}:run"

    oauth_token {
      service_account_email = google_service_account.runtime_audit_invoker.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.audit_invoker]
}
