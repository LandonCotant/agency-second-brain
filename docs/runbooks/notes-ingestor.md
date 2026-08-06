# Runbook — Samsung Notes ingestor (ADR 0031)

The notes ingestor polls two Drive folders and republishes new PDFs into
the existing Triage pipeline. The user's Tab S9 capture flow is:

1. Open the note in Samsung Notes.
2. Tap **Share** → **Save to Drive** → **PDF**.
3. Pick **Brain Inbox / Notes** (default) or **Brain Inbox / Notes-HIPAA**.

The next weekly scheduler tick (Mondays 6am Pacific) will list the file,
extract Markdown via Vertex Gemini multimodal, write a row to
`agent_outputs.notes`, publish into `asb-triage-input`, and move the file
to `processed/` inside the originating folder. To trigger an ad-hoc run,
see "Smoke test" below.

## One-time setup (out-of-band)

The ingestor SA accesses the folders via direct folder share — **NO
Domain-Wide Delegation**. ADR 0027's allowlist is unchanged.

1. **Create folders in your personal Drive** (one-time):
   - `Brain Inbox/Notes/`
   - `Brain Inbox/Notes-HIPAA/`  *(optional — only if you want a
     HIPAA-cascade-on path)*
2. **Share each folder with the ingestor SA**:
   - Right-click → **Share** → enter
     `asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com`
     → set role to **Editor** → click **Send** (uncheck "Notify people").
   - Editor is required so the ingestor can move processed files into
     a `processed/` subfolder. The ingestor never modifies or deletes
     your originals.
3. **Capture the folder ids** (visible in the URL when the folder is
   open) and apply them as Terraform variables:
   ```
   notes_default_folder_id = "<id of Brain Inbox/Notes/>"
   notes_hipaa_folder_id   = "<id of Brain Inbox/Notes-HIPAA/>"   # optional
   ```
   Then targeted apply:
   ```bash
   terraform -chdir=terraform/envs/prod apply \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_notes_ingestor
   ```

## Smoke test

After the Terraform apply + first image push (`cloudbuild.notes-ingestor.yaml`),
manually trigger the job once before letting the scheduler take over:

```bash
gcloud run jobs execute asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1
```

Then verify (≤30 s after job completion):

```bash
# 1. Corpus row landed.
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "SELECT note_id, filename, extraction_method, hipaa_isolated, page_count \
   FROM \`agency-brain-demo.agent_outputs.notes\` \
   ORDER BY ingested_at DESC LIMIT 5"

# 2. Audit row emitted (agent_id = 'notes-ingestor').
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "SELECT timestamp, success, JSON_VALUE(output, '$.decision') AS decision \
   FROM \`agency-brain-demo.agent_audit_log.events\` \
   WHERE agent_id = 'notes-ingestor' \
   ORDER BY timestamp DESC LIMIT 5"

# 3. Watermark advanced.
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "SELECT folder_path, last_modified_time_seen, updated_at \
   FROM \`agency-brain-demo.agent_state.notes_ingestor_watermark\` \
   ORDER BY updated_at DESC LIMIT 5"

# 4. Triage classified the note (within ~5 min of the ingest tick finishing,
#    once the triage bridge's next 5-min tick fires).
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "SELECT item_id, source, subject, severity \
   FROM \`agency-brain-demo.agent_outputs.triaged_items\` \
   WHERE source = 'drive' \
   ORDER BY triaged_at DESC LIMIT 5"
```

Then verify in Drive: the test file is in `Brain Inbox/Notes/processed/`.

## HIPAA path

A note saved into `Brain Inbox/Notes-HIPAA/`:

- writes a row to `agent_outputs.notes` with `hipaa_isolated = true`,
- publishes to `asb-triage-input` with
  `aspects = ["samsung_note", "hipaa_excluded"]`,
- causes the Triage Reasoning Engine to short-circuit at the BaseAgent
  HIPAA guard (ADR 0006) — `agent_audit_log.events` will show the
  classification attempt with `hipaa_guard_status = TRIPPED` and no
  `triaged_items` row will be written. **This is the desired
  posture.**

Phase 2 (Morning Brief notes section) and Phase 3 (Triage RAG) will
filter `hipaa_isolated = false` so HIPAA notes never leak into a
non-HIPAA-context surface.

## Common operations

### Re-process a single note

If extraction was poor (low `extraction_confidence` or
`extraction_method = 'failed'`), move the file back from
`<folder>/processed/` to `<folder>/`. The next tick will re-list it,
detect a new `(file_id, revision_id)` only if Drive bumped the
revision; otherwise the writer's dedup pre-check skips. To force a
fresh extraction:

```bash
# Delete the existing row first so dedup doesn't skip:
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "DELETE FROM \`agency-brain-demo.agent_outputs.notes\` \
   WHERE source_drive_file_id = '<file_id>'"
# Then move the PDF back to the source folder; next tick will reingest.
```

### Pause ingestion

```bash
gcloud scheduler jobs pause asb-notes-ingestor-weekly \
  --project=agency-brain-demo --location=us-central1
```

### Resume

```bash
gcloud scheduler jobs resume asb-notes-ingestor-weekly \
  --project=agency-brain-demo --location=us-central1
```

### Run ad-hoc (between weekly ticks)

If you want a note ingested before next Monday's tick, trigger the
job manually:

```bash
gcloud run jobs execute asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1
```

### Bulk-share backlog

`MAX_NOTES_PER_TICK=100` caps the per-tick budget — sized to drain a
typical week's worth of notes in one tick. If you accumulate more than
100 notes in a week, the next tick processes the next 100 (and so on).
The budget is tunable via the `notes_ingestor_max_per_tick` Terraform
variable.

## Cost expectations

- **Vertex Gemini multimodal** at ~$0.001/note (single-page typed/
  handwritten notes). HIPAA-eligible under Google's BAA.
- **BigQuery storage**: `agent_outputs.notes` partitions expire at
  730d (ADR 0024). Watermark table is one row per tick × 144 ticks/day
  × 2 folders ≈ 105k rows/year — small.
- **Drive storage**: nothing on Brain side. Originals stay in your
  personal Drive (or `processed/` subfolder), inside your Workspace
  storage allotment.

## Troubleshooting

### Files aren't being listed

- Confirm folder ids are correct in TF (see one-time setup).
- Confirm the folder is shared with `asb-notes-ingestor-sa@…` as
  Editor (not Viewer — Editor is required to move files).
- Check the most-recent `agent_audit_log.events` row with
  `agent_id = 'notes-ingestor'` for an error.
- Verify the watermark isn't ahead of the file's modifiedTime:
  ```bash
  bq query --project_id=agency-brain-demo --use_legacy_sql=false \
    "SELECT folder_path, last_modified_time_seen FROM \
     \`agency-brain-demo.agent_state.notes_ingestor_watermark\` \
     ORDER BY updated_at DESC LIMIT 5"
  ```
  If the watermark is set ahead of your test file's modifiedTime,
  delete the watermark row to force a full rescan:
  ```bash
  bq query --project_id=agency-brain-demo --use_legacy_sql=false \
    "DELETE FROM \`agency-brain-demo.agent_state.notes_ingestor_watermark\` \
     WHERE folder_path = '<folder_id>'"
  ```

### Extraction returns failed

- Check `extraction_notes` on the BQ row for the diagnostic.
- Re-run after fixing the underlying issue (model availability,
  oversized PDF, etc.).
- Document AI fallback is intentionally NOT wired in v1 (ADR 0031
  §2). If Gemini hallucination becomes a recurring problem, ADR 0031
  describes the migration path.

## Solutions Drive access (ADR 0048)

ADR 0048 widens the Notes Ingestor to walk the **the agency**
Shared Drive. Five new env vars carry the folder IDs; all are optional
and silently skip when unset.

### One-time setup

1. **Grant the ingestor SA Viewer on the Shared Drive.** In Drive →
   left rail → **Shared drives** → right-click **the agency** →
   **Manage members**. Add
   `asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com`
   with **Viewer** role. (Lower-privilege than `asb-librarian-sa`'s
   Content Manager grant per ADR 0045 §7 — we never write.)
2. **Capture folder IDs** for each root you want ingested. Open the
   folder in Drive; the ID is the trailing path segment in the URL.
3. **Fill in tfvars** in `terraform/envs/prod/terraform.tfvars`:
   ```hcl
   solutions_clients_folder_id          = "<05_CLIENTS id>"
   solutions_management_legal_folder_id = "<01_MANAGEMENT & LEGAL id>"
   solutions_finance_folder_id          = "<02_FINANCE & ACCOUNTING id>"
   solutions_operations_hr_folder_id    = "<03_OPERATIONS & HR id>"
   solutions_sales_marketing_folder_id  = "<04_SALES & MARKETING (Internal) id>"
   ```
   Leave any you don't want swept as `""`.
4. **Targeted apply:**
   ```bash
   terraform -chdir=terraform/envs/prod apply \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_notes_ingestor \
     -target=module.agent_runtime.google_bigquery_dataset_iam_member.tb_notes_ingestor_airtable_replica_viewer
   ```
5. **Force-fire the Job** so the first tick doesn't wait until 06:00 PT:
   ```bash
   gcloud run jobs execute asb-notes-ingestor \
     --region us-central1 --project agency-brain-demo --wait
   ```
6. **Verify in BQ:**
   ```bash
   bq query --project_id=agency-brain-demo --use_legacy_sql=false \
     "SELECT note_kind, scope, SUBSTR(markdown_content, 1, 200) AS preview \
      FROM \`agency-brain-demo.agent_outputs.notes\` \
      WHERE scope = 'agency' \
        AND ingested_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 HOUR) \
      ORDER BY ingested_at DESC LIMIT 10"
   ```
   Each row's `preview` should start with `Client: <Name>` (for
   `05_CLIENTS` files) or `Source: <path>` (for internal folders).

### HIPAA filtering

The discovery step normalizes each client folder name (e.g.
`06_CLIENT_A` → `client a`) and looks it up against
`airtable_replica.accounts` where `hipaa = TRUE`. Matching folders are
**not listed at all** — no download, no embedding, nothing in the
corpus. Non-matching folder names log at INFO so the operator can spot
drift; the folder is ingested in that case.

To retroactively shield an already-ingested client:

```bash
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  "DELETE FROM \`agency-brain-demo.agent_outputs.notes\` \
   WHERE scope = 'agency' AND markdown_content LIKE 'Client: <Name>%'"
```

### Subfolder allowlist

Per-client only these subfolders are walked (ADR 0048 §3):

- `00_ONBOARDING`
- `01_STRATEGY`
- `05_CAMPAIGNS_AND_CHANNELS`
- `07_REPORTING (EXTERNAL)`
- `08_MEETING_NOTES`

Skipped: `02_LEGAL_ADMIN`, `03_CLIENT_BRAND_LIBRARY`,
`06_DELIVERABLES`, `09_AGENT_WORKSPACE`, `00_CLIENT_TEMPLATE`.

Internal Solutions folders (Mgmt/Finance/Ops/Sales) are walked
recursively without filtering.

### Cost watch

First-tick burst on a fresh deploy is bounded by `MAX_NOTES_PER_TICK`
(default 100). With ~5 clients × 5 subfolders × tens of files each plus
~hundreds of internal files, expect 2-5 ticks to chew through the
backlog. Per-file Vertex cost is the same as Brain folder ingestion
(`gemini-2.5-flash` extract + `text-embedding-005` embed) — typically
under $0.01/file.
