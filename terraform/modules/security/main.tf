# WS-F: Security & Compliance Terraform module
#
# Resources are split across files:
# - runtime_audits.tf  (PR #2)  Cloud Run Jobs + Cloud Scheduler triggers for
#                                the four runtime audit scripts under
#                                src/agency_brain/audit/, plus their per-
#                                script SAs and custom IAM roles.
# - (PR #3, pending)             Secret Manager "agent_kill_switch" flag read
#                                by the BaseAgent class; security review +
#                                rotation runbooks; tests/security/* extras
#                                (drafts boundary, Model Armor coverage).
# - (later)                      Model Armor regional config defaults — owned
#                                by WS-G's Reasoning Engine PRs which set
#                                ModelArmorConfig directly on each engine.
#
# Notification channels are reused from terraform/modules/observability —
# specifically the existing log-based metric `asb-hipaa-guard-tripped` already
# matches the structured stdout the audit scripts emit, so PR #2 needs no
# new alert plumbing.

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
  description = "Human owner — receives security alerts"
  type        = string
}

# ---------------------------------------------------------------------------
# Cost guardrail variables (ADR 0030)
# ---------------------------------------------------------------------------

variable "billing_export_dataset" {
  description = "BigQuery dataset (in brain_project_id) holding the Cloud Billing export. ADR 0030."
  type        = string
  default     = "billing_export"
}

variable "cost_thresholds_usd" {
  description = "Per-project daily spend thresholds in USD. Projects in this map trigger COST_THRESHOLD_EXCEEDED if exceeded; projects absent are visibility-only. ADR 0030."
  type        = map(number)
  default = {
    "agency-brain-demo" = 15
    "agency-mlops-dev"  = 30
  }
}

variable "brain_alerts_chat_webhook_secret_id" {
  description = "Secret Manager short name holding the Brain Chat incoming-webhook URL (created and accessor-bound in agent_runtime; security module reuses for the daily cost summary). Empty disables the binding so the project can apply before the secret exists."
  type        = string
  default     = "second-brain-gchat-webhook"
}

# ---------------------------------------------------------------------------
# W3 hardening (2026-05-28) — Airtable schema drift audit Job
# ---------------------------------------------------------------------------

variable "airtable_base_id" {
  description = "Airtable base ID for the Operations base (default appXXXXXXXXXXXXXX). Threaded into the W3 schema-drift audit Job as AIRTABLE_BASE_ID."
  type        = string
  default     = "appXXXXXXXXXXXXXX"
}

variable "airtable_pat_secret_id" {
  description = "Secret Manager short name holding the read-only Airtable PAT (schema.bases:read + data.records:read). Reused from data_pipeline/airtable_sync.tf — same secret, separate IAM binding for the W3 audit SA."
  type        = string
  default     = "airtable-pat-prod"
}
