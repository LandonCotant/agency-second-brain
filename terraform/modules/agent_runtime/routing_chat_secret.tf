# WS-D Chat fan-out — Brain Chat incoming-webhook URL (ADR 0023).
#
# Mirrors the airtable-tasks-write-pat-prod hygiene from triage_write_pat.tf:
# the operator creates the secret + version manually (see
# docs/runbooks/routing_fanout_setup.md), TF owns only the IAM binding
# that grants asb-routing-sa secretAccessor. This avoids TF trying to
# manage a secret that already exists in prod and keeps rotation a
# Workspace-admin-only operation.
#
# The webhook URL is bearer-token-equivalent. Only asb-routing-sa gets
# secretAccessor; rotation happens by revoking the URL in Workspace and
# adding a new secret version.

variable "brain_alerts_chat_webhook_secret_id" {
  description = "Secret Manager short name holding the Brain Chat incoming-webhook URL (ADR 0023). The operator creates this secret manually before applying. Empty disables the IAM binding so the project can apply before the secret exists."
  type        = string
  default     = "second-brain-gchat-webhook"
}

resource "google_secret_manager_secret_iam_member" "brain_alerts_chat_webhook_accessor" {
  count = var.brain_alerts_chat_webhook_secret_id != "" ? 1 : 0

  project   = var.brain_project_id
  secret_id = var.brain_alerts_chat_webhook_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_routing_sa.email}"
}
