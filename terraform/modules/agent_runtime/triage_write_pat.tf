# WS-G1 Triage Agent — Airtable write PAT IAM binding (ADR 0019).
#
# The agent drafts Tasks into Operations.Tasks via Airtable's REST API.
# The PAT used for that POST is scoped narrowly: Operations.Tasks (write)
# + Operations.Projects (read for validation). Stored in Secret Manager
# manually, same hygiene as airtable-pat-prod / airtable-crm-pat-prod.
#
# The same SA (asb-agent-triage-sa) reads this secret — no new SA, smaller
# TF churn for a 2-person tool. Plan-agent recommendation; per-component
# isolation gain from a separate SA was deemed marginal vs. the operational
# cost of another identity to track.

variable "airtable_tasks_write_pat_secret_id" {
  description = "Secret Manager short name holding the Triage Agent's Airtable write PAT (ADR 0019). Empty disables the binding so the project can ship without the secret yet provisioned."
  type        = string
  default     = "airtable-tasks-write-pat-prod"
}

resource "google_secret_manager_secret_iam_member" "triage_tasks_write_pat_accessor" {
  count = var.airtable_tasks_write_pat_secret_id != "" ? 1 : 0

  project   = var.brain_project_id
  secret_id = var.airtable_tasks_write_pat_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}
