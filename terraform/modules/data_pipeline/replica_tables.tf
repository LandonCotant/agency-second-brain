# Replica tables (PR-2). Schema is derived from airtable/schema.json so the
# Python side (src/agency_brain/sync/schema_mapping.py) and the Terraform
# side stay aligned by construction. The snapshot test
# tests/unit/sync/test_schema_mapping.py::test_full_schema_snapshot is the
# load-bearing alignment check — if a future schema.json edit shifts the
# expected columns, both sides fail loudly.
#
# Lookup fields (Projects.Client HIPAA, Tasks.Project HIPAA) are filtered
# out below: they exist solely to drive Airtable filterByFormula at the
# source query (PRD §4.1 layer 2) and are not replicated to BigQuery.
#
# Per-table layout: source columns (slugified) + system columns
# (_airtable_record_id, _airtable_table_name, _airtable_last_modified,
# _sync_run_id, _synced_at, hipaa_excluded). Cluster on _airtable_record_id
# to keep the future MERGE-style sync cheap if PR-2's WRITE_TRUNCATE
# approach gets replaced down the line.

locals {
  # Map of Airtable field type → BigQuery (type, mode). Keep in sync with
  # src/agency_brain/sync/schema_mapping.py::_TYPE_MAP. Lookup fields
  # are deliberately absent so the for-expression below skips them.
  airtable_to_bq_type = {
    singleLineText        = { type = "STRING", mode = "NULLABLE" }
    multilineText         = { type = "STRING", mode = "NULLABLE" }
    richText              = { type = "STRING", mode = "NULLABLE" }
    singleSelect          = { type = "STRING", mode = "NULLABLE" }
    email                 = { type = "STRING", mode = "NULLABLE" }
    url                   = { type = "STRING", mode = "NULLABLE" }
    phoneNumber           = { type = "STRING", mode = "NULLABLE" }
    aiText                = { type = "STRING", mode = "NULLABLE" }
    multipleSelects       = { type = "STRING", mode = "REPEATED" }
    multipleRecordLinks   = { type = "STRING", mode = "REPEATED" }
    checkbox              = { type = "BOOL", mode = "NULLABLE" }
    date                  = { type = "DATE", mode = "NULLABLE" }
    dateTime              = { type = "TIMESTAMP", mode = "NULLABLE" }
    lastModifiedTime      = { type = "TIMESTAMP", mode = "NULLABLE" }
    createdTime           = { type = "TIMESTAMP", mode = "NULLABLE" }
    number                = { type = "FLOAT64", mode = "NULLABLE" }
    percent               = { type = "FLOAT64", mode = "NULLABLE" }
    currency              = { type = "FLOAT64", mode = "NULLABLE" }
    singleCollaborator    = { type = "STRING", mode = "NULLABLE" }
    multipleCollaborators = { type = "STRING", mode = "REPEATED" }
    count                 = { type = "INT64", mode = "NULLABLE" }
    autoNumber            = { type = "INT64", mode = "NULLABLE" }
  }

  schema_json = jsondecode(file("${path.module}/../../../airtable/schema.json"))

  # System columns appended to every replica table. Order mirrors
  # schema_mapping._SYSTEM_COLUMNS so cross-language diffs are obvious.
  system_columns = [
    {
      name        = "_airtable_record_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Airtable record ID (recXXXXX). Primary key for MERGE."
    },
    {
      name        = "_airtable_table_name"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Source Airtable table name (e.g. 'Clients')."
    },
    {
      name        = "_airtable_last_modified"
      type        = "TIMESTAMP"
      mode        = "NULLABLE"
      description = "Airtable's lastModifiedTime for the record. Drives incremental delta."
    },
    {
      name        = "_sync_run_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "UUID for the sync run that wrote this row. Operational debugging."
    },
    {
      name        = "_synced_at"
      type        = "TIMESTAMP"
      mode        = "REQUIRED"
      description = "When the sync run wrote this row."
    },
    {
      name        = "hipaa_excluded"
      type        = "BOOL"
      mode        = "REQUIRED"
      description = "Always FALSE in the replica — HIPAA-flagged rows are filtered at the Airtable query (PRD §4.1 layer 2). Column exists so downstream views can carry the canonical clause COALESCE(hipaa_excluded, FALSE) = FALSE that hipaa_filter_check.py enforces (PRD §4.8)."
    },
  ]

  # Build the per-table column list: source fields (excluding lookups) +
  # system columns. Mirrors schema_mapping.replica_table_schemas. Single-base
  # architecture (ADR 0020) — every table comes from schema.json directly.
  replica_table_schemas = {
    for airtable_name, table_def in local.schema_json.tables :
    trim(replace(lower(airtable_name), "/[^a-z0-9]+/", "_"), "_") => concat(
      [
        for f in table_def.fields : {
          name = trim(replace(lower(f.name), "/[^a-z0-9]+/", "_"), "_")
          type = local.airtable_to_bq_type[f.type].type
          mode = (
            local.airtable_to_bq_type[f.type].mode == "REPEATED" ? "REPEATED" :
            (try(f.required, false) ? "REQUIRED" : "NULLABLE")
          )
          description = try(f.notes, null)
        }
        if f.type != "multipleLookupValues"
      ],
      local.system_columns,
    )
  }
}

resource "google_bigquery_table" "replica" {
  for_each = local.replica_table_schemas

  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.airtable_replica.dataset_id
  table_id   = each.key

  description = "Mirror of Airtable ${each.key}. HIPAA-filtered at source (PRD §4.1 layer 2). Schema derived from airtable/schema.json (single base, ADR 0020)."

  schema              = jsonencode(each.value)
  clustering          = ["_airtable_record_id"]
  deletion_protection = true

  labels = {
    workstream = "ws-b"
    data_class = "internal"
    source     = "airtable"
  }
}

# Operational table — sync orchestrator reads/writes this each run to track
# progress. Lives in airtable_replica so the sync SA only needs writes on a
# single dataset (PRD §4.2 least-privilege).
resource "google_bigquery_table" "sync_checkpoints" {
  project    = var.brain_project_id
  dataset_id = google_bigquery_dataset.airtable_replica.dataset_id
  table_id   = "_sync_checkpoints"

  description = "Per-source-table sync run metadata. One row per Airtable table. Maintained by airtable_to_bq."

  schema = jsonencode([
    {
      name        = "airtable_table_name"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Airtable table name (e.g. 'Clients'). Primary key."
    },
    {
      name        = "last_checkpoint"
      type        = "TIMESTAMP"
      mode        = "NULLABLE"
      description = "Max(_airtable_last_modified) successfully merged. NULL on first run."
    },
    {
      name        = "last_run_id"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "UUID of the last sync run that wrote this row."
    },
    {
      name        = "last_run_at"
      type        = "TIMESTAMP"
      mode        = "NULLABLE"
      description = "When the last sync run completed for this table."
    },
  ])

  deletion_protection = true

  labels = {
    workstream = "ws-b"
    data_class = "internal"
    purpose    = "sync-metadata"
  }
}
