# Runbook — Triage Inbox + write PAT setup (ADR 0019, ADR 0020)

Pre-merge manual steps for the Triage Agent's Airtable write chain. Each step is one-time and idempotent.

> **Note:** Single-base architecture (ADR 0020). All steps target the
> Operations base only (`appXXXXXXXXXXXXXX`). The legacy Sales/CRM base
> was retired.

## Step 1 — Add `User` field to Operations.Team

In the Operations Airtable base, open the `Team` table and add:

| Setting | Value |
|---|---|
| Field name | `User` |
| Field type | `User` (Airtable's singleCollaborator) |
| Required | unchecked (optional) |

For each existing Team row, set `User` to the corresponding Workspace user. Today this is just the operator (`owner@example.com`).

Verify by:

```bash
PAT=$(gcloud secrets versions access latest --secret=airtable-pat-prod --project=agency-brain-demo)
curl -sS -H "Authorization: Bearer $PAT" \
  "https://api.airtable.com/v0/appXXXXXXXXXXXXXX/Team?maxRecords=5" \
  | python3 -c "import json,sys; [print(r['fields'].get('Name'), '→', (r['fields'].get('User') or {}).get('id')) for r in json.load(sys.stdin)['records']]"
```

Expected output: each row shows `the operator → usrXXXXXXXXX...`. Note the `usrXXX` for the next step — you'll want to confirm the sync surfaces it.

## Step 2 — Create a sentinel Account, then the Triage Inbox Project

`Projects.Account` is required. If you don't have an internal account row yet, create one first.

**Step 2a — Create an Internal Account in `Operations.Accounts`:**

| Field | Value |
|---|---|
| Company Name | `Internal — Agency` |
| Segment | `E-commerce` (any — doesn't matter operationally) |
| Status | `Active` |
| Account Manager | yourself |
| HIPAA | unchecked |

**Step 2b — Create the Triage Inbox Project in `Operations.Projects`:**

| Field | Value |
|---|---|
| Project Name | `Triage Inbox` |
| Account | `Internal — Agency` (the row from 2a) |
| Service | (any active Service Catalog row — required, value doesn't matter) |
| Phase | `Maintain` |
| Status | `Active` |
| Owner | yourself (`owner@example.com`) |

Once created, copy the `recXXX...` id from the Airtable URL or via the API:

```bash
curl -sS -H "Authorization: Bearer $PAT" \
  "https://api.airtable.com/v0/appXXXXXXXXXXXXXX/Projects?filterByFormula=%7BProject+Name%7D+%3D+%22Triage+Inbox%22" \
  | python3 -c "import json,sys; [print(r['id']) for r in json.load(sys.stdin)['records']]"
```

Save this id — it's `TB_TRIAGE_INBOX_PROJECT_ID`.

## Step 3 — Create the write PAT

In Airtable's developer hub (https://airtable.com/create/tokens), create a new Personal Access Token:

| Setting | Value |
|---|---|
| Name | `asb-brain-triage-write` |
| Scopes | `data.records:read`, `data.records:write` |
| Access | Operations base only (`appXXXXXXXXXXXXXX`) |

Copy the PAT (one-time-shown). Store it in Secret Manager via stdin:

```bash
gcloud secrets create airtable-tasks-write-pat-prod \
  --replication-policy=automatic \
  --project=agency-brain-demo
printf '%s' '<PAT>' | gcloud secrets versions add airtable-tasks-write-pat-prod \
  --data-file=- --project=agency-brain-demo
```

Verify the SA can access it:

```bash
gcloud secrets versions access latest \
  --secret=airtable-tasks-write-pat-prod \
  --project=agency-brain-demo \
  --impersonate-service-account=asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com \
  | head -c 16 && echo "...[OK]"
```

(That'll only work after the TF apply in step 5 grants the binding.)

## Step 4 — Set env vars in `terraform.tfvars`

```hcl
# terraform/envs/prod/terraform.tfvars
airtable_tasks_write_pat_secret_id = "airtable-tasks-write-pat-prod"
```

(`airtable_base_id` is already set in `terraform.tfvars`. Single-base architecture per ADR 0020.)

You'll also export `TB_TRIAGE_INBOX_PROJECT_ID` and `AIRTABLE_OPS_BASE_ID` in your shell when running `scripts/deploy_triage_re.py` — those are read at deploy time and baked into the Reasoning Engine's env vars.

## Step 5 — Targeted apply (production-touching workflow)

```bash
cd terraform/envs/prod
terraform plan -out=tfplan.bin

# Sanity check the plan: should show ONLY
#   - 1 add: google_secret_manager_secret_iam_member.triage_tasks_write_pat_accessor
#   - 1 in-place change: google_bigquery_table.sender_to_project_v (SQL changed)
#   - 1 in-place change: google_bigquery_table.replica["team"] (User column added)
#
# If you see destroys outside agent_runtime drift, STOP and investigate.

terraform apply \
  -target=module.agent_runtime.google_secret_manager_secret_iam_member.triage_tasks_write_pat_accessor \
  -target=module.data_pipeline.google_bigquery_table.sender_to_project_v \
  -target='module.data_pipeline.google_bigquery_table.replica["team"]'
```

## Step 6 — Re-deploy the Reasoning Engine

```bash
export AIRTABLE_OPS_BASE_ID=appXXXXXXXXXXXXXX
export AIRTABLE_TASKS_WRITE_PAT_SECRET_ID=airtable-tasks-write-pat-prod
export TB_TRIAGE_INBOX_PROJECT_ID=recXXX...  # from Step 2

python scripts/deploy_triage_re.py
```

The script bakes these into the deployed engine's env vars; Cloud Build re-deploys on PR merge with the same values from CI env config.

## Verification

End-to-end smoke test once the apply + redeploy is done:

```bash
# 1. Replica state — the operator's User column populated.
bq query --nouse_legacy_sql --project_id=agency-brain-demo \
  'SELECT name, workspace_email, user FROM airtable_replica.team'

# 2. View state — owner_user_id surfaced for projects whose owner has a Team row.
bq query --nouse_legacy_sql --project_id=agency-brain-demo \
  'SELECT sender_email, project_id, owner_email, owner_user_id
     FROM airtable_replica.sender_to_project_v LIMIT 5'

# 3. Resolver match — invoke the deployed RE with a sender that's in the CRM.
python scripts/deploy_triage_re.py --invoke-only --sender alice@acme.com  # if such a flag exists, else use the SDK directly

# 4. Check Airtable — a new Task row appears under the matching Project with
#    Source = "Triage Agent", Approval Status = "Drafted by Agent".

# 5. Resolver no-match — invoke with an unknown sender.
python scripts/deploy_triage_re.py --invoke-only --sender stranger@unknown-domain.example

# 6. Check Airtable — a new Task row appears under "Triage Inbox" Project.
#    Cloud Logging shows triage.no_project_match event.
```

## Saved query — "today's no-match items"

For periodic monitoring (recommend a Looker tile):

```sql
SELECT
  triaged_at,
  source,
  source_event_ref,
  reasoning,
  airtable_task_record_id
FROM `agency-brain-demo.agent_outputs.triaged_items`
WHERE DATE(triaged_at) = CURRENT_DATE()
  AND airtable_task_record_id IS NOT NULL
  -- Inbox-routed items: cross-reference the Inbox project's record id.
  -- Replace `recProjTriageInbox` with the actual id from Step 2.
ORDER BY triaged_at DESC
```

A more direct query against the Airtable side:

```bash
PAT=$(gcloud secrets versions access latest --secret=airtable-pat-prod --project=agency-brain-demo)
curl -sS -H "Authorization: Bearer $PAT" \
  "https://api.airtable.com/v0/appXXXXXXXXXXXXXX/Tasks?filterByFormula=%7BProject%7D+%3D+%22Triage+Inbox%22"
```

## Troubleshooting

- **"Owner field required" on POST** — Airtable rejected the create because Tasks.Owner is required. Check that the Team row for the Project's Owner has `User` populated; the `LEFT JOIN` in the view will yield NULL for missing Team rows, and the agent will then omit Owner on POST. Either set Team.User or temporarily make Tasks.Owner non-required in Airtable.
- **No Inbox draft on no-match signal** — confirm `TB_TRIAGE_INBOX_PROJECT_ID` is set and the Reasoning Engine was redeployed after setting it. `gcloud ai reasoning-engines describe` shows the env vars.
- **Resolver always returns None even for known senders** — confirm the materialized view has data: `bq query 'SELECT COUNT(*) FROM airtable_replica.sender_to_project_v'`. If zero, the join chain failed somewhere upstream — check that `contacts` has rows with non-null `email` and `account`, and that `accounts._airtable_record_id` matches the rec-ids referenced in `projects.account`.
