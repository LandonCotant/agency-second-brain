output "audit_sa_emails" {
  description = "Map of audit-script key → service account email. Each SA holds a per-script custom role + dataset-scoped writes on agent_audit_log."
  value       = { for k, sa in google_service_account.audit : k => sa.email }
}

output "audit_job_names" {
  description = "Map of audit-script key → Cloud Run Job name. cloudbuild updates each job's image on every main merge."
  value       = { for k, j in google_cloud_run_v2_job.audit : k => j.name }
}

output "audit_invoker_sa_email" {
  description = "Cloud Scheduler invoker SA shared across all four asb-audit-* jobs."
  value       = google_service_account.runtime_audit_invoker.email
}

output "audit_image" {
  description = "Fully-qualified Artifact Registry path for the asb-audit container image."
  value       = local.audit_image
}
