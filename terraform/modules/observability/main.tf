# WS-E: Observability Terraform module
#
# Builds the alert + dashboard layer on top of Agent Platform's native Agent
# Observability (which provides trace + cost views automatically). Scope grows
# across PRs; see README and docs/acceptance/ws-e-observability.md.
#
# PR 1 ships:
# - Email + Chat notification channels (notifications.tf)
# - HIPAA isolation alert via log-based metric on Cloud Audit Logs
#   (alerts_hipaa.tf)
#
# Deferred to PR 2 (lands after WS-C's shared interfaces merge to main):
# - agent_audit_log.events BigQuery table + IAM
# - Sync failure / agent error / audit write failure / Model Armor alerts
# - The four §8.1 dashboards

variable "brain_project_id" {
  description = "Brain project ID, output by the foundation module"
  type        = string
}

variable "region" {
  description = "Default region"
  type        = string
  default     = "us-central1"
}

variable "owner_email" {
  description = "Email address for alert notifications"
  type        = string
}

variable "chat_space_id" {
  description = "Google Chat space ID (just the suffix — e.g. AAAAEXAMPLESPACE — not the full 'spaces/...' form). The Google Cloud Monitoring Chat app must be added to this space before apply. See docs/runbooks/observability_tuning.md."
  type        = string
}
