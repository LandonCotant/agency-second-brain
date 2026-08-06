output "airtable_replica_dataset_id" {
  description = "BigQuery dataset for the Airtable replica (consumed by WS-G agents that query client/project state)"
  value       = google_bigquery_dataset.airtable_replica.dataset_id
}

output "triage_input_topic" {
  description = "Pub/Sub topic for Workspace events flowing to the Triage Agent (WS-D wires routing against this)"
  value       = google_pubsub_topic.tb_triage_input.name
}

output "triage_input_dlq_topic" {
  description = "Pub/Sub DLQ for triage-input subscriptions"
  value       = google_pubsub_topic.tb_triage_input_dlq.name
}

output "schema_drift_alerts_topic" {
  description = "Pub/Sub topic for Airtable schema drift events (WS-E adds the operator-only subscriber)"
  value       = google_pubsub_topic.tb_schema_drift_alerts.name
}

output "hipaa_excluded_aspect_type_id" {
  description = "Knowledge Catalog aspect-type ID used by the agent context guard (PRD §4.1 layer 4)"
  value       = google_dataplex_aspect_type.hipaa_excluded.aspect_type_id
}

output "aspect_type_ids" {
  description = "Map of all WS-B-defined Knowledge Catalog aspect type IDs (spec §10.2)"
  value = {
    hipaa_excluded = google_dataplex_aspect_type.hipaa_excluded.aspect_type_id
    client_ref     = google_dataplex_aspect_type.client_ref.aspect_type_id
    project_ref    = google_dataplex_aspect_type.project_ref.aspect_type_id
    owner_ref      = google_dataplex_aspect_type.owner_ref.aspect_type_id
  }
}

output "tb_sync_airtable_sa_email" {
  description = "Service account that runs the Airtable → BQ sync. WS-F least-privilege gate validates this SA holds only the custom tbSyncAirtable role plus narrow resource-scoped bindings."
  value       = google_service_account.tb_sync_airtable_sa.email
}

output "tb_airtable_sync_job_name" {
  description = "Cloud Run Job name. cloudbuild updates this job's image on every main merge."
  value       = google_cloud_run_v2_job.tb_airtable_sync.name
}

output "tb_sync_airtable_image" {
  description = "Fully-qualified Artifact Registry path for the sync container image."
  value       = local.airtable_sync_image
}

output "airtable_replica_table_ids" {
  description = "Map of replica table IDs (BQ slug → resource id). Useful for downstream queries / monitoring."
  value       = { for k, t in google_bigquery_table.replica : k => t.table_id }
}
