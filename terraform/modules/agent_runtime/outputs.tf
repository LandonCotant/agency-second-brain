output "audit_log_dataset_id" {
  description = "BigQuery dataset hosting agent audit events. Consumed by every WS-G agent module."
  value       = google_bigquery_dataset.agent_audit_log.dataset_id
}

output "audit_log_table_id" {
  description = "Fully-qualified table id for the audit events table."
  value       = "${var.brain_project_id}.${google_bigquery_dataset.agent_audit_log.dataset_id}.${google_bigquery_table.events.table_id}"
}

output "agent_outputs_dataset_id" {
  description = "BigQuery dataset hosting agent outputs (triaged_items, risk_flags, goals, goal_scores). Read by WS-D routing and every WS-G agent."
  value       = google_bigquery_dataset.agent_outputs.dataset_id
}

output "agent_outputs_table_ids" {
  description = "Map of agent_outputs.* fully-qualified table ids. WS-G agent modules grant their own SAs dataEditor on the specific table they write."
  value = {
    triaged_items = "${var.brain_project_id}.${google_bigquery_dataset.agent_outputs.dataset_id}.${google_bigquery_table.triaged_items.table_id}"
    routed_events = "${var.brain_project_id}.${google_bigquery_dataset.agent_outputs.dataset_id}.${google_bigquery_table.routed_events.table_id}"
    risk_flags    = "${var.brain_project_id}.${google_bigquery_dataset.agent_outputs.dataset_id}.${google_bigquery_table.risk_flags.table_id}"
    goals         = "${var.brain_project_id}.${google_bigquery_dataset.agent_outputs.dataset_id}.${google_bigquery_table.goals.table_id}"
    goal_scores   = "${var.brain_project_id}.${google_bigquery_dataset.agent_outputs.dataset_id}.${google_bigquery_table.goal_scores.table_id}"
  }
}

output "region" {
  description = "Region propagated for downstream Reasoning Engines and Memory Bank instance."
  value       = var.region
}

output "aiops_dashboard_sa_email" {
  description = "Service account used by the external AI Ops Dashboard (separate repo, Mac-hosted) to read agent_audit_log / agent_outputs / airtable_replica / billing_export and update agent_outputs.decisions.status. Impersonatable by owner@example.com via roles/iam.serviceAccountTokenCreator (resource-scoped)."
  value       = google_service_account.tb_aiops_dashboard_sa.email
}
