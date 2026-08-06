terraform {
  required_version = ">= 1.7.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.30"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.30"
    }
  }
}

# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
# PRD §3 + §4.6 originally called for Brain workload and audit-log sink to live
# in separate projects. The billing account hit its project-link quota during
# WS-A apply, so the audit infrastructure is collapsed into the Brain project.
# Retention lock (548 days) on the audit bucket remains the load-bearing
# protection against tampering. See ADR 0005 for the temporary deviation and
# the conditions under which we'll split projects later.

resource "google_project" "brain" {
  name            = "Agency Second Brain"
  project_id      = var.brain_project_id
  org_id          = var.org_id
  billing_account = var.billing_account

  labels = {
    workstream = "foundation"
    env        = "prod"
    component  = "brain"
  }

  auto_create_network = false
}

# ---------------------------------------------------------------------------
# API enablement on the Brain project
# ---------------------------------------------------------------------------
# PRD §5.6 week 1: only enable APIs that WS-A through WS-F need to start.
# aiplatform, bigquery, integrations, dataplex are deliberately omitted —
# they enable when WS-B/C/D/G start, scoped to those workstreams' SAs.
#
# Note: enabling cloudbuild transitively enables containerregistry → compute →
# oslogin (and containeranalysis, artifactregistry). We cannot prevent that
# chain. The side effect we care about — the auto-created Compute Engine
# default SA — is held disabled in iam.tf. See ADR 0018.

locals {
  brain_apis = [
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
    "cloudkms.googleapis.com",
    "orgpolicy.googleapis.com",
    # WS-G1 Triage Agent uses Workspace DWD with `gmail.compose` to draft
    # replies into the human reviewer's mailbox (ADR 0027). Codifies the
    # 2026-05-01 manual enablement performed during the DWD smoke.
    "gmail.googleapis.com",
    # WS-G3 Morning Brief reads today's calendar via DWD with
    # `calendar.readonly` to render the Calendar section of the brief
    # (ADR 0029). Read-only; drafts-only boundary preserved.
    # NOTE: GCP's enable-able service ID is the JSON-suffixed form,
    # NOT `calendar.googleapis.com` — Google's own naming
    # inconsistency surfaced when PR #59's targeted apply failed
    # with 403 "Not found or permission denied for service(s):
    # calendar.googleapis.com". The OAuth SCOPE remains
    # `https://www.googleapis.com/auth/calendar.readonly` (those are
    # different identifiers).
    "calendar-json.googleapis.com",
    # WS-G Samsung Notes ingestor (ADR 0031) reads PDFs from two
    # user-shared Drive folders (01_NOTES + 02_HIPAA_NOTES) via the
    # Drive v3 API. Service account `asb-notes-ingestor-sa` is
    # shared on the folders directly — no DWD scope expansion.
    # Codifies the 2026-05-02 manual enablement performed during
    # the first-deploy smoke.
    "drive.googleapis.com",
  ]
}

resource "google_project_service" "brain" {
  for_each = toset(local.brain_apis)

  project            = google_project.brain.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Org policies — applied at PROJECT scope on the two Brain projects only.
# PRD §4.1 layer 1 / §3 "no long-lived keys"; see ADR 0004 for deviation rationale.
# Project scope gives the same isolation guarantee for the Brain workload while
# leaving the ~12 other projects under example.com untouched.
# ---------------------------------------------------------------------------

locals {
  policy_target_projects = [google_project.brain.project_id]
}

resource "google_org_policy_policy" "allowed_member_domains" {
  for_each = toset(local.policy_target_projects)

  name   = "projects/${each.value}/policies/iam.allowedPolicyMemberDomains"
  parent = "projects/${each.value}"

  spec {
    rules {
      values {
        allowed_values = ["C${var.customer_id}"]
      }
    }
  }

  depends_on = [google_project_service.brain]
}

resource "google_org_policy_policy" "disable_sa_key_creation" {
  for_each = toset(local.policy_target_projects)

  name   = "projects/${each.value}/policies/iam.disableServiceAccountKeyCreation"
  parent = "projects/${each.value}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.brain]
}

resource "google_org_policy_policy" "uniform_bucket_access" {
  for_each = toset(local.policy_target_projects)

  name   = "projects/${each.value}/policies/storage.uniformBucketLevelAccess"
  parent = "projects/${each.value}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.brain]
}

resource "google_org_policy_policy" "require_os_login" {
  for_each = toset(local.policy_target_projects)

  name   = "projects/${each.value}/policies/compute.requireOsLogin"
  parent = "projects/${each.value}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.brain]
}

# ---------------------------------------------------------------------------
# Secret Manager — service enabled, no secrets created here
# ---------------------------------------------------------------------------
# WS-B creates the Airtable PAT secret; WS-G7 creates the Claude API key.
# This module only ensures the API is on.
# (See google_project_service.brain above.)
