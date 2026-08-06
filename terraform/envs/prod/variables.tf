variable "org_id" {
  description = "example.com GCP organization ID"
  type        = string
}

variable "billing_account" {
  description = "Billing account ID"
  type        = string
}

variable "customer_id" {
  description = "Cloud Identity customer ID (without the leading C — e.g. C00000000)"
  type        = string
}

variable "owner_email" {
  description = "Human owner / break-glass admin"
  type        = string
}

variable "admin_group_email" {
  description = "Optional Google Group for human admins. Leave empty until the group is provisioned in Workspace."
  type        = string
  default     = ""
}

variable "region" {
  description = "Default region"
  type        = string
  default     = "us-central1"
}

variable "chat_space_id" {
  description = "Google Chat space ID for the Brain alerts space (suffix only, e.g. AAAAEXAMPLESPACE). Cloud Monitoring app must be added to this space."
  type        = string
}

variable "airtable_pat_secret_id" {
  description = "Secret Manager short name holding the Airtable PAT (created manually before first apply per docs/runbooks/airtable_pat_rotation.md)."
  type        = string
  default     = "airtable-pat-prod"
}

variable "airtable_base_id" {
  description = "Airtable base ID the sync targets (appXXXXXXXXXXXXXX). Single base, post-ADR-0020 collapse."
  type        = string
  default     = ""
}

variable "airtable_sync_image_tag" {
  description = "Container tag for asb-airtable-sync. Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "airtable_sync_schedule" {
  description = "Cloud Scheduler cron expression for the sync job. Hourly by default — module-level description covers the rationale."
  type        = string
  default     = "0 * * * *"
}

variable "notes_default_folder_id" {
  description = "Drive folder ID for the default (non-HIPAA) Samsung Notes inbox. ADR 0031. Resolved from the URL of the user's '01_NOTES' folder shared with asb-notes-ingestor-sa as Editor."
  type        = string
  default     = ""
}

variable "notes_hipaa_folder_id" {
  description = "Drive folder ID for the HIPAA-isolated Samsung Notes inbox. ADR 0031. Optional — empty disables HIPAA-folder ingestion. Resolved from the URL of the user's '02_HIPAA_NOTES' folder shared with asb-notes-ingestor-sa as Editor."
  type        = string
  default     = ""
}

variable "notes_ingestor_image_tag" {
  description = "Container tag for asb-notes-ingestor. Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

# ADR 0037 §2 — Brain/ folder lanes. Optional in prod; populate the
# IDs after creating the Drive folders per docs/runbooks/pkm-drive-layout.md.

variable "brain_inbox_voice_folder_id" {
  description = "Drive folder ID for Brain/Inbox/Voice/. ADR 0037 §2. Empty disables voice ingestion."
  type        = string
  default     = ""
}

variable "brain_inbox_quicknotes_folder_id" {
  description = "Drive folder ID for Brain/Inbox/QuickNotes/. ADR 0037 §2. Empty disables quicknotes ingestion."
  type        = string
  default     = ""
}

variable "brain_inbox_reading_folder_id" {
  description = "Drive folder ID for Brain/Inbox/Reading/. ADR 0037 §2. Empty disables reading-folder ingestion."
  type        = string
  default     = ""
}

variable "brain_areas_folder_id" {
  description = "Drive folder ID for Brain/Areas/. ADR 0037 §2. Empty disables areas-folder ingestion."
  type        = string
  default     = ""
}

variable "brain_resources_folder_id" {
  description = "Drive folder ID for Brain/Resources/. ADR 0037 §2. Empty disables resources-folder ingestion."
  type        = string
  default     = ""
}

variable "brain_archives_folder_id" {
  description = "Drive folder ID for Brain/Archives/. ADR 0037 §2. Empty disables archives-folder ingestion."
  type        = string
  default     = ""
}

variable "brain_galaxy_folder_id" {
  description = "ADR 0054 §2 — Drive folder ID for Brain/05_GALAXY/. When set, the Librarian sweeps recursively per tick and indexes files as note_kind='galaxy'. No move, no classifier. Empty disables the sweep."
  type        = string
  default     = ""
}

variable "brain_galaxy_accounts_folder_id" {
  description = "ADR 0057 — Drive folder ID for Brain/05_GALAXY/01_ACCOUNTS/. asb-people-sync writes one .md per Airtable Account here. Empty disables the accounts side."
  type        = string
  default     = ""
}

variable "brain_galaxy_contacts_folder_id" {
  description = "ADR 0057 — Drive folder ID for Brain/05_GALAXY/02_CONTACTS/. asb-people-sync writes one .md per non-HIPAA Airtable Contact here. Empty disables the contacts side."
  type        = string
  default     = ""
}

variable "people_sync_image_tag" {
  description = "Container tag for asb-people-sync (ADR 0057). 'bootstrap' is a placeholder until cloudbuild.people-sync.yaml pushes a real one."
  type        = string
  default     = "bootstrap"
}

# ---------------------------------------------------------------------------
# ADR 0048 — the agency Shared Drive sweep
# ---------------------------------------------------------------------------

variable "solutions_clients_folder_id" {
  description = "Drive folder ID for the agency/05_CLIENTS/. ADR 0048 §3. Per-client sweep with HIPAA filter + allowlisted subfolders. Empty disables the client sweep."
  type        = string
  default     = ""
}

variable "solutions_management_legal_folder_id" {
  description = "Drive folder ID for the agency/01_MANAGEMENT & LEGAL/. ADR 0048 §2. Recursive sweep, AGENCY scope. Empty disables."
  type        = string
  default     = ""
}

variable "solutions_finance_folder_id" {
  description = "Drive folder ID for the agency/02_FINANCE & ACCOUNTING/. ADR 0048 §2. Recursive sweep, AGENCY scope. Empty disables."
  type        = string
  default     = ""
}

variable "solutions_operations_hr_folder_id" {
  description = "Drive folder ID for the agency/03_OPERATIONS & HR/. ADR 0048 §2. Recursive sweep, AGENCY scope. Empty disables."
  type        = string
  default     = ""
}

variable "solutions_sales_marketing_folder_id" {
  description = "Drive folder ID for the agency/04_SALES & MARKETING (Internal)/. ADR 0048 §2. Recursive sweep, AGENCY scope. Empty disables."
  type        = string
  default     = ""
}

variable "brain_areas_reflections_folder_id" {
  description = "Drive folder ID for Brain/Areas/Reflections/. ADR 0044 — REFLECT mode creates one Google Doc per day in this folder. Empty default keeps the v1 Gmail-draft fallback until the user creates the folder + shares it with asb-agent-triage-sa as Editor."
  type        = string
  default     = ""
}

variable "brain_inbox_drop_folder_id" {
  description = "Drive folder ID for Brain/Inbox/Drop/ (ADR 0044, Phase D Librarian). Drop files are classified + moved into Brain/Areas/<topic>/ on the daily 5am PT tick. Empty disables Drop-folder sweeping."
  type        = string
  default     = ""
}

variable "librarian_image_tag" {
  description = "Container tag for asb-librarian (ADR 0044). Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "librarian_dest_roots" {
  description = "Phase G — multi-root destination spec for the Librarian classifier. Format: 'label:folder_id' (legacy) or 'bucket=label:folder_id' (ADR 0054 — bucket ∈ {areas, resources}; 2-segment form defaults bucket=areas). Comma-separated. Empty falls back to BRAIN_AREAS_FOLDER_ID."
  type        = string
  default     = ""
}

variable "librarian_excluded_folder_names" {
  description = "Phase G — comma-separated folder display names to exclude from classifier candidates (case-insensitive). E.g. '02_FINANCE & ACCOUNTING'."
  type        = string
  default     = ""
}

variable "brain_embedding_model" {
  description = "Vertex embedding model for the notes ingestor. ADR 0038 §1. Default text-embedding-005 (768-dim)."
  type        = string
  default     = "text-embedding-005"
}

variable "risk_watcher_image_tag" {
  description = "Container tag for asb-risk-watcher. Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "evening_reflection_image_tag" {
  description = "Container tag for asb-evening-reflection. Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "captures_materializer_image_tag" {
  description = "Container tag for asb-captures-materializer (ADR 0039). Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "brag_spotter_image_tag" {
  description = "Container tag for asb-brag-spotter (ADR 0043). Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "calendar_ingester_image_tag" {
  description = "Container tag for asb-calendar-ingester (ADR 0046 / Workstream A PR A2). Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "calendar_ingester_calendars" {
  description = "Comma-separated list of calendar entries the Calendar Ingester pulls. Each entry is either `<calendar_id>` (uses the default scope `agency`) or `<calendar_id>:<scope>` for an explicit per-calendar scope (`personal` for the user's personal Google Calendar; `agency` for work; PKM merge per ADR 0037)."
  type        = string
  default     = "primary"
}

# Knowledge Surfacer (ADR 0046) + Brain API surface (ADR 0050) variables
# removed per ADR 0059 — the Cloud Run service was retired (zero usage; the
# Claude app's brain_ask MCP tool is the canonical query surface).

variable "crm_updater_image_tag" {
  description = "Container tag for asb-crm-updater (ADR 0047). Bootstrap value used until cloudbuild builds and pushes the first real image."
  type        = string
  default     = "bootstrap"
}

variable "crm_updater_inbox_project_record_id" {
  description = "Airtable Projects record id used as the 'Triage Inbox' default project for tasks drafted by the CRM Auto-updater (ADR 0047). Reuses the same record id as TB_TRIAGE_INBOX_PROJECT_ID. Empty disables the auto-updater scheduler."
  type        = string
  default     = ""
}

variable "crm_updater_airtable_pat_secret_id" {
  description = "Secret Manager short name holding the Airtable PAT for CRM writes (Tasks POST + Contacts/Accounts PATCH on Pending Updates). May share the same secret as AIRTABLE_TASKS_WRITE_PAT_SECRET_ID."
  type        = string
  default     = "airtable-tasks-write-pat-prod"
}

variable "crm_updater_disable_notes_write" {
  description = "ADR 0049 — kill switch for the Gmail-into-corpus side-effect writer. Empty (default) leaves the corpus write ON. Set to '1' or 'true' to disable without rebuilding the image."
  type        = string
  default     = ""
}
