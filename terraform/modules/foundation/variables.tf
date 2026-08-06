variable "org_id" {
  description = "GCP organization ID for example.com"
  type        = string
}

variable "billing_account" {
  description = "Billing account ID to attach both projects to"
  type        = string
}

variable "customer_id" {
  description = "Cloud Identity customer ID (e.g. C00xxxxxx). Used by iam.allowedPolicyMemberDomains."
  type        = string
}

variable "region" {
  description = "Default GCP region"
  type        = string
  default     = "us-central1"
}

variable "owner_email" {
  description = "Human owner / break-glass admin (full Workspace email)"
  type        = string
}

variable "admin_group_email" {
  description = "Optional Google Group for human admins. Empty string skips group binding (until the group exists in Workspace). The owner_email user always gets owner directly."
  type        = string
  default     = ""
}

variable "brain_project_id" {
  description = "Project ID for the Brain workload"
  type        = string
  default     = "agency-brain-demo"
}
