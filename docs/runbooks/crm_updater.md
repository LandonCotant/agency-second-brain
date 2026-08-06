# CRM Auto-updater operational runbook

How to operate the CRM Auto-updater (ADR 0047) day-to-day.

The Auto-updater fires once per day at 06:15 PT via
`asb-crm-updater-daily`. It reads `secondbrain`-labeled Gmail
messages, extracts entities, drafts to Airtable. This file covers
the manual operations that aren't automated.

## One-time Airtable schema setup

ADR 0047 designed the Auto-updater to append drafted updates to a
`Pending Updates` long-text field on both `Contacts` and `Accounts`.
The fields are declared in `airtable/schema.json` but must be
created in the live Airtable Operations base before the agent's
first end-to-end fire (otherwise the Airtable API silently drops
the field on PATCH and the operator sees no drafts on Contacts /
Accounts).

In Airtable's UI:

1. Open the **Contacts** table → click the `+` at the right edge of
   the column headers → **Long text**.
2. Name it `Pending Updates`. Description (paste exactly):
   > ADR 0047 / CRM Auto-updater. Long-text staging area where the
   > Auto-updater appends timestamped suggestions (Last Contact /
   > Next Followup / Warmth / new context). Drafts-only per PRD
   > §4.7 — humans read these and manually update the canonical
   > fields above. Non-canonical; never read back by another agent.
3. Repeat on the **Accounts** table with the same field name and
   the equivalent description (replace "Last Contact / Next Followup
   / Warmth / new context" with "new contacts seen on this account,
   context notes from emails").

Verification (run after creating the fields):

```bash
gcloud run jobs execute asb-crm-updater \
  --project=agency-brain-demo --region=us-central1 --wait

# Then in Airtable, filter Contacts and Accounts by
# "Pending Updates is not empty" — drafted blocks should appear.
```

## What's running

| Resource | Purpose |
|---|---|
| Cloud Run Job `asb-crm-updater` | The agent runtime. |
| Cloud Scheduler `asb-crm-updater-daily` | Fires the Job at 13:15 UTC = 06:15 PT. |
| BQ table `agent_outputs.crm_updater_runs` | Run history; one row per Job execution. |
| Gmail label `secondbrain` | the operator's existing relevance filter — INPUT. |
| Gmail label `secondbrain-processed` | Auto-applied dedup label — auto-managed. |
| Airtable Contacts/Accounts `Pending Updates` field | Long-text staging area for review. |
| Airtable Tasks `Source = "CRM Auto-updater"` | Drafted Tasks. |

## Re-run on demand

```bash
gcloud run jobs execute asb-crm-updater \
  --project=agency-brain-demo --region=us-central1 --wait
```

Optional cap for a small test. Set the cap on the Job spec first, then
execute plain — do NOT pass `--update-env-vars` to `gcloud run jobs
execute`. That flag creates a containerOverride which REPLACES the env
block, so required vars (`CRM_UPDATER_INBOX_PROJECT_RECORD_ID`, etc.)
go missing and the container fails on the env check. Revert the cap
after the smoke.

```bash
gcloud run jobs update asb-crm-updater \
  --project=agency-brain-demo --region=us-central1 \
  --update-env-vars=CRM_UPDATER_MAX_MESSAGES_PER_RUN=1

gcloud run jobs execute asb-crm-updater \
  --project=agency-brain-demo --region=us-central1 --wait

gcloud run jobs update asb-crm-updater \
  --project=agency-brain-demo --region=us-central1 \
  --update-env-vars=CRM_UPDATER_MAX_MESSAGES_PER_RUN=50
```

## Re-process a specific message

The Auto-updater skips messages already labeled
`secondbrain-processed`. To re-process one:

1. Find the message in Gmail (filter `label:secondbrain-processed`).
2. Remove the `secondbrain-processed` label (Edit labels →
   uncheck).
3. The label `secondbrain` should still be present (it isn't removed
   by the Auto-updater; only the `processed` sibling is added). If
   it isn't, re-add it.
4. Run the Job (per the previous section).
5. Verify the new draft appears in Airtable.

## Read the run history

```sql
SELECT
  run_id,
  started_at,
  ended_at,
  TIMESTAMP_DIFF(ended_at, started_at, SECOND) AS duration_seconds,
  messages_processed,
  drafts_created,
  errors,
  success
FROM `agency-brain-demo.agent_outputs.crm_updater_runs`
ORDER BY started_at DESC
LIMIT 20
```

## Read per-message audit rows

```sql
SELECT
  timestamp,
  output,           -- summary: tasks=N contact_updates=M account_mentions=K cost_usd=...
  cost_usd,
  latency_ms,
  hipaa_guard_status,
  success,
  error
FROM `agency-brain-demo.agent_audit_log.events`
WHERE agent_id = 'crm-updater'
  AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY timestamp DESC
LIMIT 100
```

## Pause the Auto-updater

If the Auto-updater is misbehaving (extracting nonsense, hitting
Airtable rate limits, etc.):

```bash
gcloud scheduler jobs pause asb-crm-updater-daily \
  --project=agency-brain-demo --location=us-central1
```

The Job won't fire on its schedule. Resume:

```bash
gcloud scheduler jobs resume asb-crm-updater-daily \
  --project=agency-brain-demo --location=us-central1
```

## Rotate the Airtable PAT

Same procedure as `airtable_pat_rotation.md` — the Auto-updater
reads `airtable-tasks-write-pat-prod` (default), which is the same
secret the Triage Agent's `TaskDrafter` uses. Rotation auto-affects
both.

## Iterate the extraction prompt

The system prompt is at
`src/agency_brain/prompts/crm_updater/v1.md`. To iterate:

1. Edit the prompt locally on a feature branch.
2. Run `pytest tests/unit/agents/crm_updater/test_extractor.py` to
   confirm schema parsing still works (the prompt edit doesn't
   affect the response_schema, so this should always pass — it's a
   smoke).
3. PR + merge.
4. Rebuild + roll out the image:
   ```bash
   gcloud builds submit --config=cloudbuild.crm-updater.yaml \
     --substitutions=_TAG=prompt-v1.1 .
   gcloud run jobs update asb-crm-updater \
     --image=us-central1-docker.pkg.dev/.../crm-updater:prompt-v1.1
   ```
5. Force-fire and eyeball a few drafts.

## Troubleshooting

- **403 on Gmail API.** DWD scope grant didn't propagate or was
  rolled back. Re-check `docs/dwd_scopes.md` matches the Workspace
  Admin Console; re-apply if needed (per
  `docs/runbooks/dwd_scope_expansion_2026-05.md`).
- **Airtable 422 on Tasks POST.** Likely a schema mismatch. Pull
  the latest `airtable-sync` image is current and BQ replica has
  the column the writer is trying to populate. Check
  `airtable/schema.json` matches the actual base shape.
- **`Pending Updates` field doesn't exist.** The schema bump in PR
  B1 + airtable-sync image rebuild in PR B2 didn't propagate. Verify
  the latest `airtable-sync` ran successfully:
  ```bash
  gcloud run jobs executions list --job=asb-airtable-sync \
    --project=agency-brain-demo --region=us-central1 --limit=5
  ```
- **Drafts queue piling up.** the operator needs to drain `Pending Updates`
  + Drafted Tasks during normal review. If volume exceeds review
  capacity, lower `CRM_UPDATER_MAX_MESSAGES_PER_RUN` or pause the
  scheduler temporarily.
