# Runbook: Airtable Operations Base — Bootstrap

Provision the Operations base described in [ADR 0011](../adr/0011-airtable-operations-base.md). Most of the structure is created by `scripts/bootstrap_airtable_ops_base.py` (which reads `airtable/schema.json` as the source of truth). The Meta API can't create some derived field types — those (~5 fields) you finish in the Airtable UI.

**Total time:** ~5 min script run + ~10 min manual finishing fields + ~5 min PAT/secret/tfvars wiring + ~15 min optional views = **~35 min** end to end.

## Prerequisites

- Workspace-Owner Airtable PAT (one-time, used by the script, revoke after). Scopes: `data.records:read`, `data.records:write`, `schema.bases:read`, `schema.bases:write`. Workspace-level access (not base-level — the base doesn't exist yet).
- Workspace ID — from the URL when you view your workspace home (`https://airtable.com/workspaces/wspXXXXXXXXXXXXXX`).
- Python 3.12+ (stdlib only — no extra deps).

## Phase 1 — Run the bootstrap script (~5 min)

```bash
export AIRTABLE_BOOTSTRAP_PAT='pat...'   # Workspace-Owner PAT
# Optional: override the workspace or base name
# export AIRTABLE_WORKSPACE_ID='wsp...'
# export AIRTABLE_BASE_NAME='Agency Operations'

python3 scripts/bootstrap_airtable_ops_base.py
```

The script:
1. Aborts if a base with that name already exists in the workspace.
2. Creates the base + 8 tables (Service Catalog, Team, Risk Profiles, Clients, Goals, Projects, Tasks, Goal Scores).
3. Adds all scalar/select/collaborator fields per `airtable/schema.json`.
4. Adds all `multipleRecordLinks` (forward links). Airtable auto-creates the reverse links; the script renames them to match `schema.json` where the schema specifies a custom name.
5. Logs (but does NOT create) `multipleLookupValues`, `count`, `createdTime`, and `lastModifiedTime` fields — Airtable's Meta API doesn't expose creation for these.
6. Inserts seed rows: 7 Service Catalog rows, 10 Risk Profiles rows, 1 Team row.
7. Prints the new `base_id` and the manual-finish list.

If the script fails partway, the base is partial. Delete it via Airtable UI (link in script error output) and re-run; the script is idempotent in the sense that it aborts if a same-named base exists.

## Phase 2 — Add the manual-finish fields (~10 min)

Open the new base in Airtable. Per the script's manual-finish list, you'll add these in the UI (each takes ~30 seconds):

### Load-bearing (HIPAA cascade — must add before sync runs)

1. **`Projects` → Lookup field `Client HIPAA`**
   - Click `+` to add a field on the Projects table → choose **Lookup**
   - Linked field: `Client`
   - Field to look up: `HIPAA` (from the linked Clients row)

2. **`Tasks` → Lookup field `Project HIPAA`**
   - Add a Lookup field on Tasks
   - Linked field: `Project`
   - Field to look up: `Client HIPAA` (from the linked Projects row — this is a lookup-of-a-lookup, which Airtable supports natively)

These two are the load-bearing HIPAA filter cascade per PRD §4.1 layer 2. Without them, the sync's `filterByFormula` references unknown fields and every Projects/Tasks pull returns zero rows.

### Convenience (recommended but not load-bearing)

3. **`Tasks` → Lookup field `Client`**
   - Field type: Lookup
   - Linked field: `Project`
   - Field to look up: `Client` (denormalizes the client link onto Tasks for query speed)

4. **`Projects` → Count field `Open Tasks Count`**
   - Field type: Count
   - Linked field: `Tasks`
   - Filter: `Status` ≠ `Done` AND `Status` ≠ `Cancelled`

5. **(Optional) `createdTime` and `lastModifiedTime` system fields** on the tables that need them per `schema.json`:
   - Clients: `Last Activity Timestamp` (lastModifiedTime)
   - Projects: `Created` (createdTime), `Last Modified` (lastModifiedTime)
   - Tasks: `Created` (createdTime), `Last Modified` (lastModifiedTime)
   - Goals: `Created` (createdTime)
   - Goal Scores: `Created` (createdTime)
   - Team: `Created` (createdTime)
   - Service Catalog: (none required)

   The sync code falls back gracefully to Airtable's per-record `createdTime` metadata if the explicit `Last Modified` field isn't present, so this is convenience for human review in the Airtable UI rather than a sync requirement.

## Phase 3 — Wire the read-only sync PAT (~5 min)

The sync runs as `asb-sync-airtable-sa` and reads via a separate PAT scoped to the Operations base only. (The bootstrap PAT was Workspace-Owner and gets revoked after use.)

1. Airtable account → Developer hub → Personal access tokens → Create new token
2. Name: `asb-brain-ops-sync`
3. Scopes:
   - `data.records:read` (required)
   - `schema.bases:read` (recommended — drift detector compares schema)
   - **NO write scopes**
4. Access:
   - Add base: `Agency Operations` only
   - Do NOT grant access to the Sales/CRM base (the v1 sync doesn't read it)
5. Create + copy the `pat...` value.

```bash
PROJECT=agency-brain-demo
SECRET_NAME=airtable-pat-prod

gcloud secrets create $SECRET_NAME --project=$PROJECT --replication-policy=automatic 2>/dev/null || true

printf '%s' '<paste sync PAT>' \
  | gcloud secrets versions add $SECRET_NAME --data-file=- --project=$PROJECT
```

## Phase 4 — Set the base ID in tfvars (1 min)

The `airtable_base_id` should already be set in `terraform/envs/prod/terraform.tfvars` (the bootstrap PR adds it with the value the script printed). Verify:

```bash
grep airtable_base_id terraform/envs/prod/terraform.tfvars
# expected: airtable_base_id  = "app..."
```

## Phase 5 — Build the container + apply (~15 min)

```bash
PROJECT=agency-brain-demo
gcloud builds submit --config=cloudbuild.yaml --project=$PROJECT .

cd terraform/envs/prod
terraform apply
```

The `airtable_replica.*` BigQuery tables get rebuilt with the new schema (the previous PR-2 tables are empty, so destroy + recreate is safe).

## Phase 6 — First sync + spot check (~5 min)

```bash
gcloud run jobs execute asb-airtable-sync \
  --project=agency-brain-demo --region=us-central1 --wait

bq query --project_id=agency-brain-demo --use_legacy_sql=false "
  SELECT _airtable_record_id, service_name, category, active
  FROM \`agency-brain-demo.airtable_replica.service_catalog\`
"
```

If you see the 7 Service Catalog rows, the sync is wired end-to-end.

## Phase 7 — Recommended views (~15 min — optional but valuable)

Build these in Airtable so the operational queue is usable from day 1:

### Tasks
- **My Open Tasks** — `Owner = Current User AND Status ≠ Done AND Status ≠ Cancelled AND Approval Status = Approved` — sort by Due Date asc
- **Awaiting My Approval** — `Owner = Current User AND Approval Status = Drafted by Agent` — sort by Created desc
- **Today** — `Due Date = Today AND Status ≠ Done` — group by Owner
- **Wait Aging** — `Action Type = Wait AND Wait Started < TODAY() - 14` — sort by Wait Started asc
- **By Project** — group by Project, filter `Status ≠ Done`

### Projects
- **Active Engagements** — `Status = Active` — group by Owner
- **At Risk** — `Health ≠ Green` — sort by Health desc
- **Closing This Month** — `Target End Date IS WITHIN current month`

### Goals
- **Active by Horizon** — `Status = Active` — group by Horizon
- **Stale** — `Status = Active AND Last Reviewed < TODAY() - 14`

## Revoke the bootstrap PAT

After Phase 1 succeeds, the bootstrap PAT has done its job. Go to Airtable's Developer hub → Personal access tokens → find the bootstrap PAT (`one time base build` if you used the suggested name) → **Delete**.

The sync PAT (`asb-brain-ops-sync`) remains active in Secret Manager — that's the long-lived credential per PRD §4.5 (90-day rotation cadence).

## Failure modes

If the script errors:

- **`UNSUPPORTED_FIELD_TYPE_FOR_CREATE`** — Airtable extended its restriction to a new field type since this script was written. Check the error, add the type to `SKIPPED_TYPES` in the script, surface it in the manual-finish list.
- **`INVALID_REQUEST_UNKNOWN`** on first base creation — usually a malformed `workspaceId`. Verify your `wsp...` ID matches the workspace home URL.
- **`INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND`** — the bootstrap PAT is missing a scope. Re-create with all four scopes listed in the prerequisites.
- **Partial base after a crash** — delete it via Airtable UI (the script aborts on a same-name base, so leftover state will block re-runs) and re-run.

If the sync errors after apply:

- **"Field not found" on a Lookup field** — Phase 2 wasn't completed. Add the missing Lookup field in Airtable.
- **Empty Projects/Tasks results** — `Client HIPAA` / `Project HIPAA` Lookups missing or named differently. The sync's `filterByFormula=NOT({Client HIPAA})` references the exact field name.
- See [hipaa_isolation_verification.md](./hipaa_isolation_verification.md) for the live HIPAA cascade test.
