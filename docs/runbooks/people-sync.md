# People Sync operational runbook

`asb-people-sync` materializes `airtable_replica.{accounts,contacts}` as
markdown files under `Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/` so
wikilinks like `[[Client A]]` in your Morning Brief resolve into
`notes_links` graph edges (ADR 0053). It also populates
`<!-- AUTO -->` body sections (Active engagements / Recent activity /
Open risks for accounts; Conversation log for contacts) from BQ each
tick. Spec: ADR 0057.

## What's running

- **Cloud Run Job** `asb-people-sync` (us-central1). Image
  `us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/people-sync:<tag>`;
  rolling tag tracked in `terraform/envs/prod/terraform.tfvars`
  (`people_sync_image_tag`).
- **Cloud Scheduler** `asb-people-sync-weekly` — cron `15 6 * * 0` UTC
  = Sunday 06:15 UTC (Saturday 11:15 PM PDT). Aligned with the Sunday
  8 AM PT `weekly-review-reflect` routine.
- **Service accounts**:
  - `asb-people-sync-sa` — Job runtime. BQ read on
    `airtable_replica` + `agent_outputs`; BQ write on
    `agent_audit_log`. Drive `fileOrganizer` on
    `Brain/05_GALAXY/01_ACCOUNTS/` (`FOLDER_ID_08_REDACTED`)
    and `Brain/05_GALAXY/02_CONTACTS/`
    (`FOLDER_ID_09_REDACTED`). Folder-share + ADC, NOT DWD.
  - `asb-people-sync-invoker` — Cloud Scheduler invoker (OIDC).
- **MCP tools** (local stdio server):
  - `person_summary(name_or_email)` — structured briefing on a contact.
  - `sync_people()` — shells to `gcloud run jobs execute` for
    on-demand sync between weekly ticks.

## Re-run on demand

After editing Airtable Contacts/Accounts, refresh the Brain notes
without waiting for Sunday:

```bash
gcloud run jobs execute asb-people-sync --region=us-central1 --wait
```

Or in any Claude session with the Brain MCP loaded: **"sync the people
notes"** (calls the `sync_people` tool).

Either path finishes in 15-30 seconds; check the audit row to
confirm:

```bash
bq query --use_legacy_sql=false --location=US \
  "SELECT JSON_VALUE(output, '\$.accounts.created') AS accts_created,
          JSON_VALUE(output, '\$.contacts.updated') AS cts_updated,
          JSON_VALUE(output, '\$.accounts.failed')  AS accts_failed,
          timestamp
   FROM \`agency-brain-demo.agent_audit_log.events\`
   WHERE agent_id = 'people-sync'
   ORDER BY timestamp DESC LIMIT 1"
```

## Deploy a new image

Same shape as the Librarian / Captures-Materializer build flow:

```bash
# 1. Build + push (tag = traceable identifier)
gcloud builds submit \
  --project=agency-brain-demo \
  --region=us-central1 \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=cloudbuild.people-sync.yaml \
  --substitutions=_TAG=<tag> \
  .

# 2. Bump terraform.tfvars (people_sync_image_tag = "<tag>")
# 3. terraform apply -target=module.agent_runtime.google_cloud_run_v2_job.tb_people_sync
#    OR (faster, if env vars unchanged):
gcloud run jobs update asb-people-sync \
  --region=us-central1 \
  --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/people-sync:<tag>

# 4. Smoke fire
gcloud run jobs execute asb-people-sync --region=us-central1 --wait
```

## Pause / re-enable the scheduler

```bash
# Pause
gcloud scheduler jobs pause asb-people-sync-weekly --location=us-central1

# Resume
gcloud scheduler jobs resume asb-people-sync-weekly --location=us-central1
```

The Job + SA + IAM stay deployed when paused; only the cron trigger is
suppressed. Manual `gcloud run jobs execute` still works while paused.

## Read the run history

```bash
bq query --use_legacy_sql=false --location=US \
  "SELECT
     timestamp,
     CAST(JSON_VALUE(output, '\$.accounts.created')  AS INT64) AS a_new,
     CAST(JSON_VALUE(output, '\$.accounts.updated')  AS INT64) AS a_upd,
     CAST(JSON_VALUE(output, '\$.accounts.archived') AS INT64) AS a_arc,
     CAST(JSON_VALUE(output, '\$.contacts.created')  AS INT64) AS c_new,
     CAST(JSON_VALUE(output, '\$.contacts.updated')  AS INT64) AS c_upd,
     CAST(JSON_VALUE(output, '\$.contacts.archived') AS INT64) AS c_arc,
     latency_ms
   FROM \`agency-brain-demo.agent_audit_log.events\`
   WHERE agent_id = 'people-sync'
   ORDER BY timestamp DESC LIMIT 20"
```

## Inspect a synced file

```bash
# List 01_ACCOUNTS contents
gcloud storage ls 'drive://FOLDER_ID_08_REDACTED'   # not a real gsutil path

# Via Drive UI (faster):
# https://drive.google.com/drive/folders/FOLDER_ID_08_REDACTED  (01_ACCOUNTS)
# https://drive.google.com/drive/folders/FOLDER_ID_09_REDACTED  (02_CONTACTS)
```

Each `.md` carries YAML frontmatter at the top + a body skeleton with
four sections. User prose between the H2 headers is **never
overwritten** by subsequent syncs (ADR 0057 §4 invariant). The
`<!-- AUTO -->` sections regenerate each tick.

## Troubleshooting

### "Action needed: Set BRAIN_*_FOLDER_ID" error from `update_weekly_doc`

Different problem — that error is from the **local Brain MCP server**, not
`asb-people-sync`. Fix:

```bash
# Verify env block in ~/Library/Application Support/Claude/claude_desktop_config.json
# under the "brain" MCP server. Must include:
#   "BRAIN_BRIEFS_FOLDER_ID":      "..."
#   "BRAIN_REFLECTIONS_FOLDER_ID": "..."
#   "BRAIN_REVIEWS_FOLDER_ID":     "..."

# Then restart Claude desktop app (MCP env is read at app start).
```

### Job logs `people_sync.skip` and exits 0

```bash
gcloud run jobs describe asb-people-sync --region=us-central1 --format=json \
  | jq '.spec.template.spec.template.spec.containers[0].env'
```

Look for `BRAIN_GALAXY_ACCOUNTS_FOLDER_ID` and
`BRAIN_GALAXY_CONTACTS_FOLDER_ID`. If both are empty, the Job exits
early. Set them in `terraform.tfvars` and `terraform apply` the Job
resource.

### Drive 403 on write

The runtime SA (`asb-people-sync-sa@agency-brain-demo.iam.gserviceaccount.com`)
must be `fileOrganizer` on both `01_ACCOUNTS` and `02_CONTACTS` folders.
Verify in the Drive UI: right-click → Share → confirm the SA shows up
as **Content Manager**. If missing on either folder, share it and
re-run.

### `accounts=created:0/updated:0/unchanged:0/archived:0`

Reader queried Airtable but got zero rows past the HIPAA filter. Check:

```bash
bq query --use_legacy_sql=false --location=US \
  "SELECT COUNT(*) FROM \`agency-brain-demo.airtable_replica.accounts\`
   WHERE COALESCE(hipaa, FALSE) = FALSE
     AND COALESCE(hipaa_excluded, FALSE) = FALSE"
```

Zero → Airtable sync (`asb-airtable-sync`) hasn't run or all rows are
HIPAA. Non-zero → likely a logic bug; pull the full Cloud Run logs.

### Audit row says `success: false`

```bash
bq query --use_legacy_sql=false --location=US \
  "SELECT timestamp, error, output
   FROM \`agency-brain-demo.agent_audit_log.events\`
   WHERE agent_id = 'people-sync' AND NOT success
   ORDER BY timestamp DESC LIMIT 5"
```

Common causes:
- Drive 403 (see above)
- BQ schema drift — Airtable schema changed, replica reflects new shape,
  reader hasn't been updated. Check `bq show airtable_replica.accounts`.
- ADC expired — `gcloud auth application-default login` (only relevant
  for local dev, not the deployed Job).

## Roll back

```bash
# Re-roll an older known-good image tag
gcloud run jobs update asb-people-sync \
  --region=us-central1 \
  --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/people-sync:<known-good-tag>

# Bump terraform.tfvars people_sync_image_tag to match (so TF doesn't
# drift back to the bad tag on next apply)
```

If a sync corrupted a Drive file, revert via Drive version history
(right-click file → Version history → restore previous). The body is
**never** overwritten outside `<!-- AUTO -->` sections, so corruption
should be limited to those blocks; the next clean run will rewrite
them correctly.

## What it intentionally does NOT do (yet)

- **Bidirectional sync** — Brain edits do not push back to Airtable.
  Edit warmth/next_followup in Airtable; the next sync will reflect.
- **Triage / Gmail history in conversation log** —
  `agent_outputs.triaged_items` doesn't carry email content columns
  (subject/sender/received_at). Conversation log currently shows
  calendar attendance + wikilink mentions only. Revisit when there's
  a corpus row per inbound email.
- **Archives lifecycle** — deleted Airtable rows flip frontmatter to
  `status: archived`; file is never moved or deleted (ADR 0052
  additive-only invariant).

## Related

- ADR 0057 — full design
- ADR 0042 — Airtable Contacts schema (warmth, relationship_type, etc.)
- ADR 0044 — Drive write via folder-share + ADC (the auth pattern)
- ADR 0053 — wikilinks + `related_notes` (what this enables)
- ADR 0054 §2 — Galaxy drop-to-index (the Librarian sweep that indexes
  these files into `agent_outputs.notes`)
