# Runbook — PKM Drive layout + Captures form (ADR 0037 / 0038)

The PKM merge (ADR 0037) extends the Notes Ingestor pipeline (ADR 0031)
to cover personal-knowledge captures alongside agency notes. This
runbook covers the **out-of-band** human steps required after Phase 0
ships:

1. Create the `Brain/` Drive folder taxonomy and share with the
   ingestor SA.
2. Capture folder IDs into Terraform variables.
3. Create the Airtable `Captures` table + form view.
4. Smoke-test both capture paths end-to-end.

The Cloud Run Job, BQ schema, and IAM are landed by the same PR
that ships ADR 0037 / 0038. This runbook is the connective tissue
that turns those bits into a working capture surface.

## Folder taxonomy (IPARAG-adapted, ADR 0037 §2)

Create `Brain/` at the root of your personal Drive with this
structure:

```
Brain/
├── Inbox/
│   ├── Voice/         ← voice memos (.m4a, .mp3, .wav)
│   ├── QuickNotes/    ← short markdown captures from any source
│   ├── Reading/       ← saved articles, PDFs, web clippings
│   └── HIPAA/         ← any HIPAA-bearing capture (existing folder)
├── Areas/             ← ongoing reference (responsibilities, profiles)
├── Resources/         ← static templates, frameworks, reusables
└── Archives/          ← cold storage (completed projects, deprecated)
```

Each folder maps to a `note_kind` and `scope` on the BQ row:

| Folder | `note_kind` | `scope` | Triage publish? | HIPAA isolated? |
|---|---|---|---|---|
| `Brain/Inbox/Voice/`      | `inbox`    | `personal` | yes | no |
| `Brain/Inbox/QuickNotes/` | `inbox`    | `personal` | yes | no |
| `Brain/Inbox/Reading/`    | `inbox`    | `personal` | yes | no |
| `Brain/Inbox/HIPAA/`      | `inbox`    | `personal` | yes (with `hipaa_excluded` aspect) | yes |
| `Brain/Areas/`            | `area`     | `personal` | no  | no |
| `Brain/Resources/`        | `resource` | `personal` | no  | no |
| `Brain/Archives/`         | `archive`  | `personal` | no  | no |

**Why kind-gated triage?** Inbox folders are actionable captures; the
Triage Agent's "what work needs attention" rubric applies. Areas /
Resources / Archives are reference material — synthesized, slow-moving
content that the embedder + future Connector job (ADR 0038 §5)
surfaces semantically rather than via Triage. See ADR 0037 §6.

## One-time setup (out-of-band)

The ingestor SA accesses the folders via direct folder share — **NO
Domain-Wide Delegation**. ADR 0027's allowlist is unchanged.

### 1. Create the folder structure

In your personal Drive, create `Brain/` and the seven subfolders
listed above. The exact names matter for the runbook only — the
ingestor reads folder IDs, not paths — but matching keeps things
sane during troubleshooting.

`Brain/Inbox/HIPAA/` is the same folder as the existing
`Brain Inbox/Notes-HIPAA/` from ADR 0031. **Move or rename the
existing folder rather than creating a new one** so the existing
HIPAA notes corpus stays accessible without re-ingestion.

### 2. Share each folder with the ingestor SA

For each of the seven folders, right-click → **Share** → enter:

```
asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com
```

Set role to **Editor** → click **Send** (uncheck "Notify people").

Editor is required so the ingestor can move processed files into
a `processed/` subfolder. The ingestor never modifies or deletes
your originals.

If you share `Brain/` itself with permission inheritance (rather
than each subfolder individually), the seven sub-shares happen
automatically. Either approach works.

### 3. Capture folder IDs into Terraform variables

The folder ID is in the URL when you open the folder in Drive
(`https://drive.google.com/drive/folders/<ID_HERE>`).

Edit `terraform/envs/prod/terraform.tfvars` and set:

```hcl
brain_inbox_voice_folder_id      = "<id of Brain/Inbox/Voice/>"
brain_inbox_quicknotes_folder_id = "<id of Brain/Inbox/QuickNotes/>"
brain_inbox_reading_folder_id    = "<id of Brain/Inbox/Reading/>"
brain_areas_folder_id            = "<id of Brain/Areas/>"
brain_resources_folder_id        = "<id of Brain/Resources/>"
brain_archives_folder_id         = "<id of Brain/Archives/>"
# Existing variable; rebind to the relocated/renamed HIPAA folder.
notes_hipaa_folder_id            = "<id of Brain/Inbox/HIPAA/>"
```

Then targeted apply per `feedback_prod_touching_workflow.md`:

```bash
terraform -chdir=terraform/envs/prod apply \
  -target=module.agent_runtime.google_cloud_run_v2_job.tb_notes_ingestor
```

This rebinds the Cloud Run Job's env vars to the new folder IDs.
The job's image is upgraded in a separate step (see "Image rebuild"
in ADR 0037 §Manual operational steps).

## Airtable Captures form setup

The Captures form is the fast-text capture surface (ADR 0037 §3).
Build it once; bookmark the public form URL on your phone for
30-second-or-less text captures.

### 1. Create the `Captures` Airtable table

In the Operations base (`appXXXXXXXXXXXXXX`), create a new table
called `Captures` with these fields:

| Field name | Field type | Required | Notes |
|---|---|---|---|
| `Captured At` | createdTime | yes | Auto-populated; not user-editable |
| `Note Text` | multilineText | yes | Free-form capture body |
| `Kind` | singleSelect | yes | Options: `note`, `decision`, `win`, `todo` |
| `Scope Hint` | singleSelect | yes | Options: `personal`, `agency` |
| `Synced` | checkbox | no | Set by the sync job after BQ write |
| `Synced At` | dateTime | no | Set by the sync job |

`Synced` + `Synced At` are bookkeeping for the delete-after-sync
guard (ADR 0037 §3 closing note). The sync job writes to BQ first,
sets `Synced=true` + `Synced At=NOW()`, and only deletes Airtable
rows after they've been observed `Synced=true` for ≥1 sync cycle
(15 min). This prevents data loss if BQ INSERT succeeds but the
DELETE call fails.

### 2. Build a public form view

In the new `Captures` table:

1. Click **Create…** → **Form view**.
2. Name it `Capture` (the URL slug becomes the bookmark target).
3. Show only `Note Text`, `Kind`, `Scope Hint` (hide `Captured At`,
   `Synced`, `Synced At` from the form).
4. Default `Scope Hint` to `personal`. Default `Kind` to `note`.
5. Click **Open form** → copy the public URL.
6. Bookmark the URL on your phone's home screen.

Optional: in the form's notification settings, send yourself an
email confirmation on submit. Useful for the first week while you
verify the round-trip works; can disable later.

### 3. `airtable/schema.json` already has Captures

As of ADR 0039 (Phase 0b), `airtable/schema.json` already declares the
`Captures` table and `src/agency_brain/sync/airtable_to_bq.py`
includes it in `SYNC_TABLES_ORDER`. Once the user has created the
table + form per §1–2 above, the next `asb-airtable-sync-15m` tick
will replicate it into `airtable_replica.captures`. Confirm with:

```bash
bq query --use_legacy_sql=false \
  'SELECT COUNT(*) FROM `agency-brain-demo.airtable_replica.captures`'
```

If the count is non-zero (after at least one form submission), the
materializer's next 15-min tick will pick the row up.

### 4. Captures materializer (ADR 0039 §1)

The Cloud Run Job `asb-captures-materializer` (every 15 min, `Etc/UTC`,
matching `asb-airtable-sync-15m`) reads `airtable_replica.captures`
where `synced = FALSE`, dispatches each row by Kind:

| Kind | Action |
|---|---|
| `note` | INSERT `agent_outputs.notes` (with inline `text-embedding-005` embed) + Pub/Sub publish to `asb-triage-input` |
| `decision` | INSERT `agent_outputs.decisions` (status='draft', 30/90/365d review dates) |
| `win` | INSERT `agent_outputs.wins` (week_of = ISO Monday) |
| `todo` | Pub/Sub publish to `asb-triage-input` only |

After dispatch: flip `Synced=TRUE` + `Synced At=today`, then DELETE
the Airtable row. ADR 0039 §3 covers the failure modes — all paths
are idempotent on the deterministic dedup key
(`captures-{airtable_record_id}` for notes,
`captures-decision-{record_id}` / `captures-win-{record_id}`).

Manual one-shot trigger (e.g., for smoke testing):

```bash
gcloud run jobs execute asb-captures-materializer \
  --project=agency-brain-demo --region=us-central1
```

Verification after a `note`-Kind submission:

```bash
bq query --use_legacy_sql=false '
  SELECT note_id, note_kind, scope, ARRAY_LENGTH(embedding) AS dim
  FROM `agency-brain-demo.agent_outputs.notes`
  WHERE note_id LIKE "captures-%"
  ORDER BY ingested_at DESC LIMIT 5
'
```

Expect `note_kind='inbox'`, `scope='personal'`, `dim=768`.

## Smoke test

After Phase 0 deploys (image rebuild + targeted apply), validate
each capture path end-to-end before relying on it.

### Test 1 — PDF (existing behavior, regression check)

Drop a 1-page PDF in `Brain/Inbox/Reading/`. Trigger the ingestor:

```bash
gcloud run jobs execute asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1
```

Verify (within ~30 s of job completion):

```sql
SELECT note_id, filename, note_kind, scope, hipaa_isolated,
       extraction_method, ARRAY_LENGTH(embedding) AS embedding_dim,
       embedding_model
FROM `agency-brain-demo.agent_outputs.notes`
ORDER BY ingested_at DESC
LIMIT 1
```

Expect: `note_kind = 'inbox'`, `scope = 'personal'`,
`hipaa_isolated = false`, `embedding_dim = 768`,
`embedding_model = 'text-embedding-005'`.

### Test 2 — Markdown file (new behavior)

Drop a `test.md` file in `Brain/Inbox/QuickNotes/` containing 2-3
paragraphs. Trigger the job. Verify the same SQL above; expect:
`extraction_method = 'markdown_passthrough'` (no LLM call;
content piped through unchanged).

### Test 3 — Google Doc (new behavior)

Create a Google Doc in `Brain/Areas/` with some prose. Trigger
the job. Verify: `note_kind = 'area'`, `scope = 'personal'`,
`extraction_method = 'gemini-2.5-flash-doc-export'`. Confirm the
note does **not** publish to triage:

```sql
SELECT COUNT(*) FROM `agency-brain-demo.agent_outputs.triaged_items`
WHERE source = 'drive'
  AND triaged_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 MINUTE)
```

Expect 0 rows for the Areas folder note (kind-gated triage; ADR
0037 §6).

### Test 4 — Voice memo (new behavior)

Record a 30-second voice memo on your phone, save to
`Brain/Inbox/Voice/`. Trigger the job. Verify:
`extraction_method = 'gemini-2.5-flash-audio'`,
`markdown_content` contains a transcript with timestamps,
`note_kind = 'inbox'` (so it triggers triage), and embedding is
populated.

### Test 5 — Captures form round-trip

Open the bookmarked Capture form on your phone. Submit a quick
text note ("test capture from phone"), Kind=`note`,
Scope Hint=`personal`.

Within 15 min (next sync tick), verify:

```sql
SELECT * FROM `agency-brain-demo.airtable_replica.captures`
WHERE note_text LIKE 'test capture from phone%'
ORDER BY captured_at DESC LIMIT 1
```

Expect the row to exist. Then within another 15 min (next
materializer tick), verify the row is in `agent_outputs.notes`:

```sql
SELECT note_id, markdown_content, note_kind, scope, embedding_model
FROM `agency-brain-demo.agent_outputs.notes`
WHERE markdown_content LIKE 'test capture from phone%'
ORDER BY ingested_at DESC LIMIT 1
```

Within ~30 min total (one full sync cycle past the BQ write), the
Airtable row should be deleted (`Synced` was true for ≥1 cycle).

### Test 6 — Idempotency

Re-trigger the job (Test 1's PDF still in `processed/`). Verify
no duplicate rows land — the `(source_drive_file_id, revision_id)`
dedup pre-check + the `embedding_content_hash` guard combine to
make a re-run a no-op.

## HIPAA path

A capture saved into `Brain/Inbox/HIPAA/`:

- writes a row to `agent_outputs.notes` with
  `hipaa_isolated = TRUE`, `note_kind = 'inbox'`,
  `scope = 'personal'`,
- publishes to `asb-triage-input` with
  `aspects = ["samsung_note", "hipaa_excluded"]`,
- causes the Triage Reasoning Engine to short-circuit at the
  BaseAgent HIPAA guard (ADR 0006) — `agent_audit_log.events`
  shows the classification attempt with
  `hipaa_guard_status = TRIPPED` and no `triaged_items` row is
  written.

Embeddings still get generated for HIPAA notes (the embedding
model is HIPAA-eligible under Google's BAA, like Gemini), but
all readers (Reflection, Brag Spotter, Connector) filter
`hipaa_isolated = FALSE` so HIPAA content never bleeds into a
non-HIPAA-context surface.

## Common operations

Most operations from `notes-ingestor.md` apply unchanged. PKM-specific
operations:

### Promote an inbox note to Galaxy

Galaxy notes are atomic synthesis pieces (ADR 0037 §2). Promotion is
a column flip:

```sql
UPDATE `agency-brain-demo.agent_outputs.notes`
SET note_kind = 'galaxy'
WHERE note_id = '<id>'
```

The Phase 5 Connector job (deferred) reads `note_kind = 'galaxy'`
preferentially when surfacing semantic links. No file move; no
re-embed.

### Re-embed a note (force re-embedding)

Triggers the §4 idempotency guard's miss branch:

```sql
UPDATE `agency-brain-demo.agent_outputs.notes`
SET embedding_content_hash = NULL,
    embedding_model = NULL
WHERE note_id = '<id>'
```

Next time the ingestor runs (or the Phase 0 backfill job runs
manually), the row gets a fresh embedding.

### Backfill embeddings for pre-ADR-0038 rows

After the Phase 0 schema migration applies, existing rows from
ADR 0031 ingests will have `embedding IS NULL`. Run the one-shot
backfill via `BACKFILL_MODE=embeddings_only` (ADR 0039 §4). Flip
the env var on the Job spec first, then execute plain — passing
`--update-env-vars` to `gcloud run jobs execute` REPLACES the env
block via containerOverrides instead of merging, which strips the
other required vars and crashes the container.

```bash
gcloud run jobs update asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1 \
  --update-env-vars=BACKFILL_MODE=embeddings_only

gcloud run jobs execute asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1 --wait

# Revert when the corpus is fully embedded.
gcloud run jobs update asb-notes-ingestor \
  --project=agency-brain-demo --region=us-central1 \
  --update-env-vars=BACKFILL_MODE=none
```

Each invocation processes up to `MAX_BACKFILL_PER_TICK` rows
(default 100). Repeat the `execute` step until the corpus is fully
embedded; then revert via the third command above (or just leave
it at `none`, the default — the next scheduled tick runs the normal
folder loop again).

Verify after the run:

```bash
bq query --use_legacy_sql=false '
  SELECT
    COUNTIF(embedding IS NULL OR ARRAY_LENGTH(embedding) = 0) AS missing,
    COUNTIF(embedding_model IS NULL) AS missing_model,
    COUNT(*) AS total
  FROM `agency-brain-demo.agent_outputs.notes`
'
```

Expect `missing = 0` and `missing_model = 0`. Per-row audit rows
land in `agent_audit_log.events` with `JSON_VALUE(output,
'$.decision') = 'backfill_embedded'` for the inspectable trail.

## Cost expectations

In addition to ADR 0031 §Cost expectations:

- **Vertex `text-embedding-005`** at ~$0.0001/note. Capped by the
  Notes Ingestor's existing per-tick budget.
- **Audio extraction** via Gemini multimodal — comparable cost to
  PDF extraction (~$0.001/memo for a typical 30-second to 2-minute
  voice memo).
- **BQ `VECTOR_SEARCH`** — pay-per-query, ~$0.005/search. See ADR
  0038 §6 for envelope.
- **Airtable Captures table** — included in existing PAT scope; no
  new cost.

Total PKM-additive Vertex spend at expected steady-state: well
under $5/month. Verified against the daily-spend tripwire (ADR 0030
prod $15/day) on first 7 days post-deploy.

## Troubleshooting

### A folder isn't being polled

- Check the folder ID is in `terraform.tfvars` and applied.
- Confirm the folder is shared with `asb-notes-ingestor-sa@…` as
  Editor.
- Check audit log:
  ```sql
  SELECT timestamp, JSON_VALUE(output, '$.error') AS error
  FROM `agency-brain-demo.agent_audit_log.events`
  WHERE agent_id = 'notes-ingestor'
    AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
  ORDER BY timestamp DESC
  LIMIT 5
  ```

### Audio extraction returns gibberish

- Audio quality matters more than file format. Re-record in a quiet
  environment.
- Check `extraction_confidence` on the BQ row; <0.5 indicates the
  model couldn't transcribe cleanly.
- Long audio (>5 min) may exceed Gemini's audio context. Phase 0
  caps audio file size at 10 MB; longer recordings should be split.

### Captures form submits but doesn't reach BQ

- Check `airtable_replica.captures` first — sync runs every 15 min,
  so the row appears there before reaching `agent_outputs.notes`.
- Then check the Captures materializer audit log:
  ```sql
  SELECT timestamp, JSON_VALUE(output, '$.error')
  FROM `agency-brain-demo.agent_audit_log.events`
  WHERE agent_id = 'captures-materializer'
  ORDER BY timestamp DESC LIMIT 5
  ```
- If the BQ row exists but Airtable row isn't deleted, the
  delete-after-sync guard is working as designed (waiting for
  one more sync cycle confirmation).

### Embeddings backfill is slow

The Phase 0 backfill processes existing rows sequentially via
`text-embedding-005`. For ~100 existing notes, expect 1–2 minutes;
for ~1000, expect 10–15 minutes. The job is idempotent; if it's
killed mid-run, re-running picks up where it left off (rows with
`embedding IS NULL` are still NULL).

## Reflection Docs (ADR 0044)

Evening Reflection v2's REFLECT mode (9pm PT) writes one Google Doc
per day into `Brain/Areas/Reflections/`. The Doc contains today's
signals + standard + custom reflection questions + voice memo
extracts + an empty space for the user to type into. A Chat card
with the Doc's URL replaces the Gmail-draft notification.

### One-time setup

1. **Create folder.** In Drive, under `Brain/Areas/`, create a folder
   named `Reflections`.

2. **Share with the SA.** Share `Brain/Areas/Reflections/` with
   `asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com`
   as **Editor**. (This SA already has Editor on the rest of `Brain/`
   per ADR 0044 §1; the Reflections folder is the *new* surface that
   needs the same grant.)

3. **Capture the folder ID** into `terraform/envs/prod/terraform.tfvars`:
   ```hcl
   brain_areas_reflections_folder_id = "<id of Brain/Areas/Reflections/>"
   ```

4. **Build + push image** (a fresh image ships ADR 0044 code):
   ```bash
   gcloud builds submit \
     --project=agency-brain-demo \
     --region=us-central1 \
     --default-buckets-behavior=regional-user-owned-bucket \
     --config=cloudbuild.evening-reflection.yaml \
     --substitutions=_TAG=adr-0044-reflection-doc-v1 \
     .
   ```

5. **Targeted apply.** Update `evening_reflection_image_tag` in
   tfvars to match the tag built in step 4, then:
   ```bash
   terraform -chdir=terraform/envs/prod apply \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_evening_reflection \
     -target=module.agent_runtime.google_secret_manager_secret_iam_member.tb_evening_reflection_chat_secret_accessor \
     -target=module.agent_runtime.google_bigquery_table.evening_reflections
   ```

6. **Smoke-fire** REFLECT mode on demand. Flip the Job's `REFLECTION_MODE`
   env first, then execute plain — `gcloud run jobs execute
   --update-env-vars=...` REPLACES the env block via containerOverrides
   instead of merging, so other required env vars disappear and the
   container fails. Revert the mode after.
   ```bash
   gcloud run jobs update asb-evening-reflection \
     --project=agency-brain-demo --region=us-central1 \
     --update-env-vars=REFLECTION_MODE=reflect

   gcloud run jobs execute asb-evening-reflection \
     --project=agency-brain-demo --region=us-central1 --wait

   # Revert so the scheduler's containerOverride for PROMPT/REFLECT
   # remains the authoritative mode-switch path.
   gcloud run jobs update asb-evening-reflection \
     --project=agency-brain-demo --region=us-central1 \
     --update-env-vars=REFLECTION_MODE=reflect
   ```
   (Note: the scheduler-side containerOverride for REFLECTION_MODE
   continues to work because the invoker SA has the
   `run.jobs.runWithOverrides` permission and the scheduler's
   containerOverride sets only the mode env, with everything else
   pulled from the Job's default env block. The sharp edge is the
   operator-side `gcloud run jobs execute` flag.)

7. **Verify**:
   - Drive: `Brain/Areas/Reflections/<today>.gdoc` exists with all 7 sections.
   - BQ: `reflection_doc_id` + `reflection_doc_url` populated for
     today's REFLECT row.
   - Chat: card with Doc link in `Brain alerts`.
   - Gmail Drafts: NO new "Evening Reflection — …" draft (the 4pm
     PROMPT-mode draft from earlier in the day should still be there).

### Fallback

If `brain_areas_reflections_folder_id` is empty (or the folder share
is missing), REFLECT mode falls back to the Gmail-draft surface so
the daily ritual never breaks during rollout. Once the folder + share
land, REFLECT mode automatically switches to the Doc surface on the
next tick.

## Drop folder + Librarian (ADR 0044 + 0045, daily-reflection-doc Phases D & G)

The Librarian is the "drop a file in and let an AI sort it" agent.
Daily 5am PT, it lists `01_BRAIN_INBOX/06_DROP/` in the **Agency
Brain Shared Drive**, classifies each file via Gemini against
candidates pulled from BOTH `Agency Second Brain/05_AREAS` AND
`the agency/05_CLIENTS` (the user's actual client work
taxonomy), then routes to the correct destination subfolder.

Drop is **sort-only** — files there do NOT publish to Triage.
QuickNotes stays the actionable-inbox surface (publishes to Triage
on ingestion). Folder semantics:

| Folder (Agency Second Brain Shared Drive) | Use for | Triage publish? | Librarian move? |
|---|---|---|---|
| `01_BRAIN_INBOX/06_DROP/`       | "I don't know where this goes — file it" | **no**  | **yes** |
| `01_BRAIN_INBOX/03_VOICE/`      | voice memos                                | yes     | no |
| `01_BRAIN_INBOX/04_READING/`    | articles to triage                         | yes     | no |
| `01_BRAIN_INBOX/02_HIPAA_NOTES/`| HIPAA-bearing capture                      | yes (with isolation) | **never** |
| `01_BRAIN_INBOX/01_NOTES/`      | legacy Samsung Notes path (ADR 0031)       | yes (with isolation guard) | no |
| `01_BRAIN_INBOX/05_AREAS/01_REFLECTIONS/` | daily Reflection Docs (Phase B writes here) | n/a | no |

Phase G destination roots (set in tfvars):

```hcl
librarian_dest_roots = "brain:<05_AREAS folder id>,clients:<Solutions/05_CLIENTS id>"
librarian_excluded_folder_names = "02_FINANCE & ACCOUNTING"
```

Multi-root means the Librarian can route a file into either:
  - `brain/<topic>` (your `05_AREAS` subfolders for personal-scope reference)
  - `clients/<client>/<sub-template>` (e.g. `06_CLIENT_A/08_MEETING NOTES`)

Per-file the Librarian also:

  1. **Ingests to `agent_outputs.notes`** (Phase G — `note_kind='area'`,
     `scope='agency'` for clients root, `scope='personal'` for brain root,
     embedded via `text-embedding-005`). Once ingested, the file becomes
     RAG corpus for the Reflection Doc and future agents.
  2. **Links** the new note to its top-K semantic neighbors via
     `VECTOR_SEARCH` (writes bidirectional rows into
     `agent_outputs.notes_links`).
  3. **Auto-edits the destination dossier's `## Related` section** when
     a `dossier.gdoc` exists in the destination folder.
  4. **Renames anonymous-pattern filenames** to the canonical convention
     `YYYY-MM-DD_<topic-slug>_<short-description>.<ext>` (only when the
     original looks auto-generated like `Notes_260502_*.pdf`,
     `Untitled.md`, `IMG_NNNN.jpg`, `Screenshot ...`. Deliberate names
     preserved.)
  5. **Archives the original** into `06_DROP/processed/` after
     successful copy-fallback (cross-Shared-Drive moves often need to
     copy + leave-original — same-Drive archive avoids the 403 path).

### One-time setup

1. **Create the `06_DROP/` folder** under `01_BRAIN_INBOX/` in the
   Agency Second Brain Shared Drive.

2. **Add `asb-librarian-sa` as Content Manager on BOTH Shared Drives:**
   - **Agency Second Brain** Shared Drive (whole drive — Drop +
     QuickNotes + 05_AREAS all live there).
   - **the agency** Shared Drive (whole drive — provides
     `05_CLIENTS/<client>/<sub-template>/` as classification
     destinations).

   Right-click each Shared Drive → **Manage members** → add
   `asb-librarian-sa@agency-brain-demo.iam.gserviceaccount.com` as
   **Content Manager**. Drive UI will require "Notify people"
   checked even though the SA's email isn't a real mailbox; that's a
   harmless quirk — the membership grant works regardless.

   **HIPAA isolation**: per-folder share is the load-bearing layer
   for `02_HIPAA_NOTES`. Even though the SA has Drive-membership
   access, the lister's `_FORBIDDEN_FOLDER_ROLES = {hipaa}` guard
   refuses to ever process the HIPAA folder, AND the env-var
   allowlist excludes it, AND `02_HIPAA_NOTES` should NOT be set as
   a Librarian destination root. Defense-in-depth.

3. **Capture the folder IDs into tfvars:**
   ```hcl
   brain_inbox_drop_folder_id = "<id of 01_BRAIN_INBOX/06_DROP/>"

   # Phase G — multi-root destination spec.
   librarian_dest_roots = "brain:<05_AREAS id>,clients:<Solutions/05_CLIENTS id>"
   librarian_excluded_folder_names = "02_FINANCE & ACCOUNTING"
   ```

4. **Build + push image:**
   ```bash
   gcloud builds submit \
     --project=agency-brain-demo \
     --region=us-central1 \
     --default-buckets-behavior=regional-user-owned-bucket \
     --config=cloudbuild.librarian.yaml \
     --substitutions=_TAG=adr-0044-librarian-v1 \
     .
   ```

5. **Set image tag** in `librarian_image_tag` (tfvars) and apply:
   ```bash
   terraform -chdir=terraform/envs/prod apply \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_librarian \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_librarian_daily \
     -target=module.agent_runtime.google_service_account.tb_librarian_sa \
     -target=module.agent_runtime.google_project_iam_custom_role.tb_librarian
   ```

6. **Smoke-fire:** drop a markdown file describing one of your
   accounts (e.g. Client C Studio) into `Brain/Inbox/Drop/`, then:
   ```bash
   gcloud run jobs execute asb-librarian \
     --project=agency-brain-demo --region=us-central1
   ```

7. **Verify:**
   - Drive activity sidebar shows the file moved into
     `Brain/Areas/clients/client-c-studio/` (or `_uncategorized/`
     if confidence < 0.6).
   - Audit row:
     ```sql
     SELECT JSON_VALUE(output, '$.dest_folder_path') AS dest,
            JSON_VALUE(output, '$.confidence') AS conf,
            JSON_VALUE(output, '$.neighbors_linked') AS n,
            JSON_VALUE(output, '$.related_section_updated') AS rel
     FROM `agency-brain-demo.agent_audit_log.events`
     WHERE agent_id = 'librarian'
     ORDER BY timestamp DESC LIMIT 5
     ```
   - `agent_outputs.notes_links` has new bidirectional rows for the
     file's top-3 semantic neighbors.
   - Destination dossier doc (if present) shows the auto-edited
     `## Related` section between the marker comments.

8. **Unpause the daily scheduler** (it ships paused so the first
   run is the operator-driven smoke-fire above):
   ```bash
   gcloud scheduler jobs resume asb-librarian-daily \
     --location=us-central1 --project=agency-brain-demo
   ```

## Areas/ taxonomy starter

The Areas folder is "your wiki" — long-lived reference material that
the Librarian sorts new captures into and that Reflection / future
Morning Brief readers surface as RAG context. There's no required
shape, but a starter structure that lots of people land on:

```
Brain/Areas/
├── clients/
│   ├── clienta-pi/
│   │   ├── dossier.gdoc
│   │   └── (notes the Librarian sorts in over time)
│   ├── client-c-studio/
│   │   └── dossier.gdoc
│   └── agency-solutions/
│       └── dossier.gdoc
├── playbooks/
│   ├── local-service/
│   │   └── dossier.gdoc      ← onboarding playbook
│   └── e-commerce/
│       └── dossier.gdoc
├── sops/                      ← Standard operating procedures
│   ├── risk-watcher-thresholds.gdoc
│   └── triage-routing.gdoc
├── personal/                  ← personal-scope reference
│   └── (your own dossiers)
├── Reflections/               ← daily Reflection Docs land here (ADR 0044)
└── _uncategorized/            ← Librarian fallback (auto-created)
```

Empty folders cost nothing — create the ones that match how you
already think about your work; let the Librarian populate them as
you drop files.

## Dossier template

Each `dossier.gdoc` is the index doc for one Areas topic. The
Librarian's auto-edited `## Related` section requires two marker
comments inside the doc; everything else is yours to shape.

A starter dossier looks like:

```
# Client C Studio — dossier

## Overview

(One paragraph: who, what, why this account matters.)

## Active engagements

- (Project / contract details, refreshed manually)

## Notes

(Free-form notes. Can be empty — the Librarian will sort dropped
files into this folder, and they show up under Related below.)

## Related

<!-- librarian:related:start -->
(no related notes yet)
<!-- librarian:related:end -->
```

The two `<!-- librarian:related:* -->` HTML-style comments are the
load-bearing markers. The Librarian rewrites everything between them
on each tick (when a file lands in this folder); content elsewhere
in the doc is never touched. If the markers are missing, the
Librarian appends a new `## Related` section at the doc end on the
first run — but starting with the markers in place keeps things
clean.

To create a new dossier:
1. In Drive, navigate to `Brain/Areas/<topic>/` (create the folder
   if it doesn't exist).
2. New → Google Docs → blank document.
3. Rename the doc to `dossier`.
4. Paste the template above; replace placeholder text.
5. Save (Drive auto-saves).
6. Share the parent folder with `asb-librarian-sa@…` as Editor (if
   not already shared via parent inheritance).

The Librarian picks up dossiers automatically — no env-var or TF
change needed. Each new `dossier.gdoc` becomes a candidate destination
on the next tick.

## References

- ADR 0031 — Notes Ingestor (parent runbook: `notes-ingestor.md`)
- ADR 0037 — PKM merge architecture
- ADR 0038 — Embeddings + `VECTOR_SEARCH`
- ADR 0044 — Drive write via folder share (Reflection Docs + Librarian)
- ADR 0027 — DWD scope allowlist (unchanged)
- ADR 0006 — BaseAgent HIPAA guard (HIPAA path behavior)
