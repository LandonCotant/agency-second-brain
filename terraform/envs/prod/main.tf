provider "google" {
  region                = var.region
  user_project_override = true
  billing_project       = "agency-brain-demo"
}

provider "google-beta" {
  region                = var.region
  user_project_override = true
  billing_project       = "agency-brain-demo"
}

module "foundation" {
  source = "../../modules/foundation"

  org_id            = var.org_id
  billing_account   = var.billing_account
  customer_id       = var.customer_id
  owner_email       = var.owner_email
  admin_group_email = var.admin_group_email
  region            = var.region
}

module "agent_runtime" {
  source = "../../modules/agent_runtime"

  brain_project_id                = module.foundation.brain_project_id
  region                          = var.region
  notes_default_folder_id         = var.notes_default_folder_id
  notes_hipaa_folder_id           = var.notes_hipaa_folder_id
  notes_ingestor_image_tag        = var.notes_ingestor_image_tag
  risk_watcher_image_tag          = var.risk_watcher_image_tag
  evening_reflection_image_tag    = var.evening_reflection_image_tag
  captures_materializer_image_tag = var.captures_materializer_image_tag
  brag_spotter_image_tag          = var.brag_spotter_image_tag
  airtable_base_id                = var.airtable_base_id

  # ADR 0037 / 0038 — PKM merge: Brain/ folder lanes + embedding model.
  brain_inbox_voice_folder_id      = var.brain_inbox_voice_folder_id
  brain_inbox_quicknotes_folder_id = var.brain_inbox_quicknotes_folder_id
  brain_inbox_reading_folder_id    = var.brain_inbox_reading_folder_id
  brain_areas_folder_id            = var.brain_areas_folder_id
  brain_resources_folder_id        = var.brain_resources_folder_id
  brain_archives_folder_id         = var.brain_archives_folder_id
  brain_embedding_model            = var.brain_embedding_model

  # ADR 0048 — the agency Shared Drive sweep. All five are
  # optional; unset = silently skipped by the ingestor at runtime.
  solutions_clients_folder_id          = var.solutions_clients_folder_id
  solutions_management_legal_folder_id = var.solutions_management_legal_folder_id
  solutions_finance_folder_id          = var.solutions_finance_folder_id
  solutions_operations_hr_folder_id    = var.solutions_operations_hr_folder_id
  solutions_sales_marketing_folder_id  = var.solutions_sales_marketing_folder_id

  # ADR 0044 — Reflection-as-Doc: parent folder for daily Reflection Docs.
  # Empty default = REFLECT mode keeps Gmail-draft fallback until the
  # user creates Brain/Areas/Reflections/ and shares it with
  # asb-agent-triage-sa.
  brain_areas_reflections_folder_id = var.brain_areas_reflections_folder_id

  # ADR 0044 + plan Phase D — Librarian: Drop folder + image tag.
  brain_inbox_drop_folder_id = var.brain_inbox_drop_folder_id
  librarian_image_tag        = var.librarian_image_tag

  # Phase G — multi-root destination spec + folder-name exclusions.
  librarian_dest_roots            = var.librarian_dest_roots
  librarian_excluded_folder_names = var.librarian_excluded_folder_names

  # ADR 0054 §2 — Galaxy capture surface. Optional; empty disables.
  brain_galaxy_folder_id = var.brain_galaxy_folder_id

  # ADR 0057 — Personal CRM bridge. asb-people-sync writes per-row .md
  # files under Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/. Both folder
  # IDs empty = sync disabled.
  brain_galaxy_accounts_folder_id = var.brain_galaxy_accounts_folder_id
  brain_galaxy_contacts_folder_id = var.brain_galaxy_contacts_folder_id
  people_sync_image_tag           = var.people_sync_image_tag

  # ADR 0046 — Calendar ingester (Workstream A PR A2).
  calendar_ingester_image_tag = var.calendar_ingester_image_tag
  calendar_ingester_calendars = var.calendar_ingester_calendars

  # ADR 0046 Knowledge Surfacer Cloud Run service + ADR 0050 Brain API
  # surface retired per ADR 0059 (the Claude app + brain_ask MCP tool is
  # the canonical query surface; the /ask Chat app + /api/ask route had
  # zero usage and no internal callers). The shared retriever class stays
  # in src as a library for brain_ask.

  # ADR 0047 — CRM Auto-updater (Workstream B PR B2).
  crm_updater_image_tag               = var.crm_updater_image_tag
  crm_updater_inbox_project_record_id = var.crm_updater_inbox_project_record_id
  crm_updater_airtable_pat_secret_id  = var.crm_updater_airtable_pat_secret_id

  # ADR 0049 — Gmail-into-corpus kill switch. Empty (default) = ON.
  crm_updater_disable_notes_write = var.crm_updater_disable_notes_write

  # ADR 0061 — Triage bridge runs classification in-process (RE retired). It
  # drafts Tasks into the same Triage Inbox project as the CRM Auto-updater.
  triage_inbox_project_record_id = var.crm_updater_inbox_project_record_id
}

module "data_pipeline" {
  source = "../../modules/data_pipeline"

  brain_project_id        = module.foundation.brain_project_id
  region                  = var.region
  airtable_pat_secret_id  = var.airtable_pat_secret_id
  airtable_base_id        = var.airtable_base_id
  airtable_sync_image_tag = var.airtable_sync_image_tag
  airtable_sync_schedule  = var.airtable_sync_schedule
}

module "observability" {
  source = "../../modules/observability"

  brain_project_id = module.foundation.brain_project_id
  region           = var.region
  owner_email      = var.owner_email
  chat_space_id    = var.chat_space_id
}

module "security" {
  source = "../../modules/security"

  brain_project_id = module.foundation.brain_project_id
  region           = var.region
  owner_email      = var.owner_email
}

output "brain_project_id" {
  value = module.foundation.brain_project_id
}

output "agent_audit_log_table" {
  value = module.agent_runtime.audit_log_table_id
}

output "agent_outputs_dataset" {
  value = module.agent_runtime.agent_outputs_dataset_id
}

output "agent_outputs_tables" {
  value = module.agent_runtime.agent_outputs_table_ids
}

output "airtable_replica_dataset_id" {
  value = module.data_pipeline.airtable_replica_dataset_id
}

output "triage_input_topic" {
  value = module.data_pipeline.triage_input_topic
}

output "schema_drift_alerts_topic" {
  value = module.data_pipeline.schema_drift_alerts_topic
}

output "aspect_type_ids" {
  value = module.data_pipeline.aspect_type_ids
}

output "tb_sync_airtable_sa_email" {
  value = module.data_pipeline.tb_sync_airtable_sa_email
}

output "tb_airtable_sync_job_name" {
  value = module.data_pipeline.tb_airtable_sync_job_name
}

output "tb_sync_airtable_image" {
  value = module.data_pipeline.tb_sync_airtable_image
}

output "airtable_replica_table_ids" {
  value = module.data_pipeline.airtable_replica_table_ids
}

output "audit_sa_emails" {
  value = module.security.audit_sa_emails
}

output "audit_job_names" {
  value = module.security.audit_job_names
}

output "audit_invoker_sa_email" {
  value = module.security.audit_invoker_sa_email
}

output "audit_image" {
  value = module.security.audit_image
}
