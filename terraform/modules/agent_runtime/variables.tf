variable "brain_project_id" {
  description = "Brain project ID, output by the foundation module"
  type        = string
}

variable "region" {
  description = "Default region (BigQuery dataset location)"
  type        = string
  default     = "us-central1"
}

# ADR 0039 — passed through to the captures-materializer Cloud Run Job for
# the pyairtable client base id. Default empty supports the bootstrap case
# where the prod tfvars hasn't wired in the Operations base id yet.
variable "airtable_base_id" {
  description = "Airtable Operations base id (appXXX). Used by the captures materializer to mutate Captures rows after BQ write."
  type        = string
  default     = ""
}
