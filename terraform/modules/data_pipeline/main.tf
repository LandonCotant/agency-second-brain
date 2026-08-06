# WS-B: Data Pipeline Terraform module
#
# PR-1 scope:
#   - Enable bigquery + dataplex APIs (foundation already enabled pubsub)
#   - airtable_replica BigQuery dataset (tables come with PR-2 sync code)
#   - Pub/Sub: asb-triage-input (+ DLQ), asb-schema-drift-alerts
#   - Knowledge Catalog (Dataplex) aspect types from spec §10.2
#
# Out of scope for PR-1 (intentionally deferred):
#   - asb-sync-airtable-sa + custom IAM role  → PR-2 with sync code
#   - airtable_replica.* table DDL           → PR-2 (derived from airtable/schema.json)
#   - Vantage federation + ADR               → PR-3
#   - Workspace publishers + DWD scopes      → PR-4
#   - Aspect application to specific entries → as data lands in PR-2/4
#
# References: PRD §3, §4.1, §6.2; spec §3.1, §9.1, §10.2.

variable "brain_project_id" {
  description = "Brain project ID, output by the foundation module"
  type        = string
}

variable "region" {
  description = "Default region (used for Dataplex aspect-type location)"
  type        = string
  default     = "us-central1"
}

# ---------------------------------------------------------------------------
# API enablement
# ---------------------------------------------------------------------------
# Foundation enabled pubsub but deliberately omitted bigquery + dataplex
# (foundation main.tf comment: "scoped to those workstreams' SAs").

locals {
  data_pipeline_apis = [
    "bigquery.googleapis.com",
    "dataplex.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
}

resource "google_project_service" "data_pipeline" {
  for_each = toset(local.data_pipeline_apis)

  project            = var.brain_project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# BigQuery datasets
# ---------------------------------------------------------------------------
# airtable_replica is the destination for the airtable_to_bq sync. Per
# PRD §4.1 layer 2, HIPAA-flagged clients (and any Project/Task linked to
# them) are excluded at the source query level — never post-hoc. Schema
# drift surfaces to asb-schema-drift-alerts and is never auto-applied
# (PRD §6.2). The _sync_checkpoints table lives inside this dataset so
# the sync SA only needs writes on a single dataset.

resource "google_bigquery_dataset" "airtable_replica" {
  project    = var.brain_project_id
  dataset_id = "airtable_replica"
  location   = "US"

  description = "Replica of the Airtable operational base. HIPAA-filtered at source. Source-of-truth schema: airtable/schema.json. See PRD §6.2."

  delete_contents_on_destroy = false

  labels = {
    workstream = "ws-b"
    data_class = "internal"
  }

  depends_on = [google_project_service.data_pipeline]
}

# ---------------------------------------------------------------------------
# Pub/Sub topics
# ---------------------------------------------------------------------------
# asb-triage-input
#   - WS-B publishes Workspace events here (PR-4); WS-G1 subscribes (later).
#   - Created in PR-1 because WS-D depends on the topic name to wire routing.
#
# asb-triage-input-dlq
#   - Dead-letter destination for the triage subscription. The subscription
#     itself lands with the publisher code in PR-4; topic exists now so the
#     subscription can reference it.
#
# asb-schema-drift-alerts
#   - airtable_to_bq publishes here (PR-2) when it detects new Airtable
#     columns. the operator-only subscriber lands in WS-E.

resource "google_pubsub_topic" "tb_triage_input" {
  project = var.brain_project_id
  name    = "asb-triage-input"

  message_retention_duration = "604800s" # 7 days

  labels = {
    workstream = "ws-b"
    purpose    = "triage-input"
  }
}

resource "google_pubsub_topic" "tb_triage_input_dlq" {
  project = var.brain_project_id
  name    = "asb-triage-input-dlq"

  message_retention_duration = "604800s"

  labels = {
    workstream = "ws-b"
    purpose    = "triage-input-dlq"
  }
}

resource "google_pubsub_topic" "tb_schema_drift_alerts" {
  project = var.brain_project_id
  name    = "asb-schema-drift-alerts"

  message_retention_duration = "604800s"

  labels = {
    workstream = "ws-b"
    purpose    = "schema-drift"
  }
}

# ---------------------------------------------------------------------------
# Knowledge Catalog (Dataplex) aspect types
# ---------------------------------------------------------------------------
# Spec §10.2 defines four aspect types. Aspect type *definitions* live here
# (the schema for the tag); aspect *application* to specific entries — BQ
# tables, Drive folders, Gmail threads — happens in later PRs as those
# entries materialize.
#
# asb-hipaa-excluded is load-bearing: PRD §4.1 layer 4 has the agent context
# guard abort on any input carrying this aspect. The other three (client,
# project, owner) are reference aspects pointing back at canonical Airtable
# record IDs.

resource "google_dataplex_aspect_type" "hipaa_excluded" {
  project        = var.brain_project_id
  location       = var.region
  aspect_type_id = "asb-hipaa-excluded"

  description = "Marks entries the Brain must never ingest. Agent context guard (PRD §4.1 layer 4) aborts on inputs carrying this aspect."

  metadata_template = jsonencode({
    name = "tb_hipaa_excluded"
    type = "record"
    recordFields = [
      {
        name  = "reason"
        type  = "string"
        index = 1
        annotations = {
          description = "Why excluded (e.g. client.HIPAA=true, folder under hipaa-clients/)"
        }
      },
      {
        name  = "tagged_at"
        type  = "datetime"
        index = 2
        annotations = {
          description = "When the aspect was applied"
        }
      },
    ]
  })

  depends_on = [google_project_service.data_pipeline]
}

resource "google_dataplex_aspect_type" "client_ref" {
  project        = var.brain_project_id
  location       = var.region
  aspect_type_id = "asb-client-ref"

  description = "Reference to the canonical Airtable Client record. Spec §10.2 'Client' aspect."

  metadata_template = jsonencode({
    name = "tb_client_ref"
    type = "record"
    recordFields = [
      {
        name  = "airtable_record_id"
        type  = "string"
        index = 1
        annotations = {
          description = "Airtable Clients record ID (rec...)"
        }
      },
      {
        name  = "client_name"
        type  = "string"
        index = 2
        annotations = {
          description = "Denormalized client name for human readability"
        }
      },
    ]
  })

  depends_on = [google_project_service.data_pipeline]
}

resource "google_dataplex_aspect_type" "project_ref" {
  project        = var.brain_project_id
  location       = var.region
  aspect_type_id = "asb-project-ref"

  description = "Reference to the canonical Airtable Project record. Spec §10.2 'Project' aspect."

  metadata_template = jsonencode({
    name = "tb_project_ref"
    type = "record"
    recordFields = [
      {
        name  = "airtable_record_id"
        type  = "string"
        index = 1
        annotations = {
          description = "Airtable Projects record ID"
        }
      },
      {
        name  = "project_name"
        type  = "string"
        index = 2
        annotations = {
          description = "Denormalized project name for human readability"
        }
      },
    ]
  })

  depends_on = [google_project_service.data_pipeline]
}

resource "google_dataplex_aspect_type" "owner_ref" {
  project        = var.brain_project_id
  location       = var.region
  aspect_type_id = "asb-owner-ref"

  description = "Owner attribution for Tasks, Triaged Items, Projects. Spec §10.2 'Owner' aspect."

  metadata_template = jsonencode({
    name = "tb_owner_ref"
    type = "record"
    recordFields = [
      {
        name  = "team_record_id"
        type  = "string"
        index = 1
        annotations = {
          description = "Airtable Team record ID"
        }
      },
      {
        name  = "email"
        type  = "string"
        index = 2
        annotations = {
          description = "Workspace email of the owner"
        }
      },
    ]
  })

  depends_on = [google_project_service.data_pipeline]
}
