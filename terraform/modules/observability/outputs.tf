# PR 2's alerts (sync failure, agent error rate, audit log write failure,
# Model Armor spike) reuse these channel IDs. Exposing them as outputs lets
# PR 2 reference module.observability.email_channel_id without re-discovering
# resource names.

output "email_channel_id" {
  description = "Cloud Monitoring notification channel ID for owner email"
  value       = google_monitoring_notification_channel.email_owner.id
}

output "chat_channel_id" {
  description = "Cloud Monitoring notification channel ID for Brain alerts Chat space"
  value       = google_monitoring_notification_channel.chat_brain_alerts.id
}

output "hipaa_guard_metric_name" {
  description = "Log-based metric name for HIPAA_GUARD_TRIPPED events; PR 2 dashboards may chart this alongside agent_audit_log.events queries."
  value       = google_logging_metric.hipaa_guard_tripped.name
}
