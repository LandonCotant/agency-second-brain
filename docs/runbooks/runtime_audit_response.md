# Runbook: Runtime audit response

The five runtime audit Cloud Run Jobs verify the security + cost charters
(PRD §4 + ADRs 0024/0028/0030) hold against the **live** state on a
continuous cadence. Each run emits one row to `agent_audit_log.events`
plus one structured stdout line. The HIPAA isolation alert in
`terraform/modules/observability/alerts_hipaa.tf` matches
`jsonPayload.event:"HIPAA_GUARD_TRIPPED"`; the cost-threshold alert in
`alerts_cost.tf` matches `jsonPayload.event:"COST_THRESHOLD_EXCEEDED"`.
A breach trips the corresponding Chat ping with no extra wiring.

This runbook is the operator playbook: what each script catches, what to do
when it fires, and how to keep the baselines fresh.

## Inventory

| Job (Cloud Run) | Module | Schedule (UTC) | Detects | PRD/ADR ref |
|---|---|---|---|---|
| `asb-audit-sensitive-isolation` | `agency_brain.audit.hipaa_isolation_check` | hourly (`0 * * * *`) | HIPAA-flagged data in any Brain BQ table | §4.1 layer 5 |
| `asb-audit-sensitive-iam-drift` | `agency_brain.audit.hipaa_iam_drift` | daily 06:00 (`0 6 * * *`) | Brain-project IAM diverged from baseline | §4.1 layer 1 |
| `asb-audit-drafts-boundary` | `agency_brain.audit.drafts_boundary_check` | nightly 03:00 (`0 3 * * *`) | Any SA holding a forbidden role; DWD allowlist drift | §4.7 / ADRs 0027, 0029 |
| `asb-audit-bucket-iam-drift` | `agency_brain.audit.bucket_iam_drift` | daily 04:00 (`0 4 * * *`) | New GCS bucket binding | ADR 0005 |
| `asb-audit-cost-daily-check` | `agency_brain.audit.daily_spend_check` | daily 09:00 (`0 9 * * *`) | Per-project daily spend over threshold | ADR 0030 |

All five run from the `asb-audit:bootstrap` container in
`us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/`. Each job's
Cloud Run `command` overrides the entrypoint to invoke a specific module.

## Event taxonomy

Every run emits exactly one event. `agent_id = audit-<short>` joins it to
the script.

| `event_id` | Meaning | Operator action |
|---|---|---|
| `SECURITY_AUDIT_OK` | Clean run, no drift. | None. |
| `HIPAA_GUARD_TRIPPED` | HIPAA-flagged row in a Brain table. | **P0** — see §"HIPAA isolation breach" below. |
| `SECURITY_DRIFT` | IAM / bucket / boundary drift. | Investigate within 24h; either revert or update baseline. |
| `SECURITY_AUDIT_ERROR` | Script failed before completing. | Read Cloud Run Job logs, fix the underlying error, retrigger manually. |
| `COST_AUDIT_OK` | Daily spend run completed, all monitored projects under their thresholds. | None. (A daily Chat card is still posted with per-project totals.) |
| `COST_THRESHOLD_EXCEEDED` | At least one monitored project exceeded its configured daily threshold. | See §"Cost threshold exceeded" below. |

Query the last 24h of audit events:

```bash
bq query --project_id=agency-brain-demo --use_legacy_sql=false '
  SELECT timestamp, agent_id, success, JSON_EXTRACT_SCALAR(output, "$.event_id") AS event_id, error
  FROM `agency-brain-demo.agent_audit_log.events`
  WHERE agent_id LIKE "audit-%"
    AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  ORDER BY timestamp DESC
'
```

## Per-script response

### HIPAA isolation breach

`asb-audit-sensitive-isolation` found HIPAA-flagged rows in `airtable_replica.*`
or `agent_outputs.*`. The existing P0 alert is firing in the Brain alerts
Chat space.

1. **Halt agent execution** (PR #3 will automate this; until then, manual):
   ```bash
   # WS-F PR #3 will create the agent-kill-switch secret. Until then, the
   # P0 alert text in alerts_hipaa.tf instructs operator action; no agents
   # are deployed in this state of the build, so the practical action is
   # to pause any in-progress agent rollout PRs.
   ```

2. **Find the breach scope.** The audit row's `output` JSON carries
   `counts` and a `samples` block:
   ```bash
   bq query --project_id=agency-brain-demo --use_legacy_sql=false '
     SELECT timestamp, output FROM `agency-brain-demo.agent_audit_log.events`
     WHERE agent_id = "audit-hipaa-isolation"
       AND JSON_EXTRACT_SCALAR(output, "$.event_id") = "HIPAA_GUARD_TRIPPED"
     ORDER BY timestamp DESC LIMIT 1
   '
   ```

3. **Verify against the source.** Open Airtable's Operations base and confirm
   the matched record IDs are actually `HIPAA = true`. If yes, the sync
   filter regressed; check `src/agency_brain/sync/hipaa_filters.py` and
   the Lookup fields in `airtable/schema.json`. If no, the BQ row is stale
   and the next sync cycle (≤ 15 min) will clear it.

4. **Document.** Open an incident note before resuming. Capture: timestamp,
   matched record IDs, root cause, time-to-clear.

### IAM drift (`asb-audit-sensitive-iam-drift`)

A binding in the brain project differs from
`terraform/modules/security/expected/brain_iam_baseline.json`.

1. **Read the diff:**
   ```bash
   gcloud run jobs execute asb-audit-sensitive-iam-drift \
     --project=agency-brain-demo --region=us-central1 \
     --args=python,-m,agency_brain.audit.hipaa_iam_drift,--diff --wait
   gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="asb-audit-sensitive-iam-drift"' \
     --limit=20 --project=agency-brain-demo
   ```

2. **Decide:** is the new state correct?
   - **Yes** (TF was applied, baseline is stale): refresh the baseline
     (see "Baseline rotation" below).
   - **No** (someone clicked in the console): revoke via console or
     `gcloud projects remove-iam-policy-binding` and the next run will
     pass.

3. **Known gap:** this script does NOT verify "Brain SAs have no IAM in the
   HIPAA project" (PRD §4.1 layer 1's cross-project assertion). That would
   require granting the audit SA `getIamPolicy` on a peer project, which
   contradicts the SA-isolation we want to preserve. Cover it quarterly
   instead:
   ```bash
   gcloud asset search-all-iam-policies \
     --scope=organizations/000000000000 \
     --query='memberTypes:serviceAccount AND policy:agency-brain-demo' \
     --format='value(resource,policy.bindings.role,policy.bindings.members)'
   ```
   Expect: every binding's `resource` is `agency-brain-demo` (no rows
   on HIPAA or Vantage projects).

### Drafts boundary breach (`asb-audit-drafts-boundary`)

Some service account holds `roles/owner`, `roles/editor`, or a
`*.admin`-pattern role.

1. **Identify the offending (role, member) pair** from the audit row's
   `output.violations` array.
2. **Revoke immediately** if you didn't intend the binding:
   ```bash
   gcloud projects remove-iam-policy-binding agency-brain-demo \
     --role=<role> --member=<member>
   ```
3. **If intentional**, write an ADR (next free number 0013) documenting
   why the predefined high-privilege role is required and update
   `scripts/least_privilege_check.py`'s allowlist + the brain IAM
   baseline. Note: granting a predefined high-privilege role to an SA is
   a strong PRD §4.2 signal — needs the operator's sign-off.

**Known gap:** Workspace OAuth scope check (`gmail.send` / `gmail.modify`)
is not implemented. DWD isn't provisioned yet; reintroduce when WS-G1 ships
the Triage Agent with `gmail.compose` scope.

### Bucket IAM drift (`asb-audit-bucket-iam-drift`)

A new GCS bucket exists, an existing bucket disappeared, or a binding
changed.

1. **Read the diff:**
   ```bash
   gcloud run jobs execute asb-audit-bucket-iam-drift \
     --project=agency-brain-demo --region=us-central1 \
     --args=python,-m,agency_brain.audit.bucket_iam_drift,--diff --wait
   ```

2. **For new buckets**, identify the creator (Cloud Audit Logs `storage.buckets.create`).
   Most legitimate cases are either Terraform-driven (state bucket already
   in baseline) or Cloud Build's working bucket (pattern
   `<project-number>-cloudbuild`). Either is OK to add to the baseline.
3. **For new principals on an existing bucket**, treat as IAM drift and
   apply the same revoke-or-document logic.

### Cost threshold exceeded (`asb-audit-cost-daily-check`)

One or more monitored projects (`agency-brain-demo`,
`agency-mlops-hipaa`, `agency-mlops-dev`) exceeded its configured daily
threshold. The same job already posted a Chat card with per-project
totals + top services; the alert email is the loud follow-up.

1. **Identify the breaching project + service:**
   ```bash
   gcloud logging read 'jsonPayload.event="COST_THRESHOLD_EXCEEDED"' \
     --project=agency-brain-demo --limit=1 --format=json \
     | jq '.[0].jsonPayload.summary'
   ```

   The `breaches` array has each over-threshold project with its top 5
   services by cost. The `totals_usd` map has the full per-project
   breakdown for context.

2. **Decide:** is the spend legitimate (e.g. mlops-dev burning during
   a training run) or anomalous (the 2026-05-02 orphan-RE pattern)?
   - **Legitimate:** raise the threshold for that project — edit
     `cost_thresholds_usd` in `terraform/modules/security/main.tf`,
     targeted apply on `module.security.google_cloud_run_v2_job.audit['cost_daily_check']`.
   - **Anomalous:** investigate the top services. For Vertex AI spikes,
     `gcloud ai reasoning-engines list` (or the equivalent REST call —
     see ADR 0028 alert docs) and delete orphans; for BQ, check for
     runaway queries; for Cloud Run, look for a hot-loop scheduler.

3. **If you decide to halt the project entirely** (manual kill switch
   per ADR 0030):
   ```bash
   bash scripts/disable_billing.sh <PROJECT_ID>
   ```
   Prompts for confirmation; prints the relink command on completion.

4. **Re-arm the alert.** It auto-closes after 7 days; manual close in
   Cloud Monitoring → Alerting if you've resolved sooner.

## Baseline rotation

Both IAM-drift checks compare against checked-in JSON at
`terraform/modules/security/expected/`. Refresh whenever the live state
legitimately changes:

```bash
gcloud auth application-default login

python -m agency_brain.audit.hipaa_iam_drift --capture > \
  terraform/modules/security/expected/brain_iam_baseline.json

python -m agency_brain.audit.bucket_iam_drift --capture > \
  terraform/modules/security/expected/bucket_iam_baseline.json

# Eyeball the diff. Anything unexpected = the signal — investigate before committing.
git diff terraform/modules/security/expected/

# Commit alongside the Terraform change that caused the drift.
```

The first time you run this, the baseline files will go from `{}` to a full
snapshot. That's the bootstrap. After that, every legitimate IAM change
produces a small diff that's reviewable in PR.

## Airtable-side HIPAA cross-check (quarterly, manual)

`hipaa_isolation_check` catches **filter-bypass** (HIPAA rows that landed
in BQ despite the filter). It does not catch **silent suppression** (the
sync ran successfully but, for unrelated reasons, dropped a HIPAA row that
was never in BQ to begin with — there's nothing to count). Manual cross-check
once a quarter:

```bash
# 1. Pull the canonical HIPAA=true client IDs straight from Airtable
#    (the bootstrap PAT was revoked; use the long-lived sync PAT in
#    Secret Manager `airtable-pat-prod`).
PAT=$(gcloud secrets versions access latest --secret=airtable-pat-prod --project=agency-brain-demo)
curl -s "https://api.airtable.com/v0/appXXXXXXXXXXXXXX/Clients?filterByFormula=%7BHIPAA%7D" \
  -H "Authorization: Bearer $PAT" \
  | jq '[.records[].id] | sort'

# 2. Confirm every ID is absent from airtable_replica.clients.
bq query --project_id=agency-brain-demo --use_legacy_sql=false '
  SELECT _airtable_record_id FROM `agency-brain-demo.airtable_replica.clients`
  WHERE _airtable_record_id IN UNNEST([<paste IDs from step 1>])
'
# Expect: zero rows.
```

A nonzero count is a P0; treat as a HIPAA isolation breach.

## Building + pushing a new image

Use `cloudbuild.audit.yaml` (committed at the repo root). It pins the Cloud
Build SA (`asb-cloud-build-sa`, since ADR 0018 disabled the legacy default)
and templates the image tag via the `_TAG` substitution. Job names are the
full `asb-audit-<task>-<scope>` form (e.g. `asb-audit-drafts-boundary`, NOT
the short `asb-audit-drafts-bnd` the SA uses).

```bash
cd "/path/to/repo"

# Build + push. _TAG should tie the image to the change that produced it
# (mirrors PR #50 / #52 / #54 pattern).
gcloud builds submit \
  --project=agency-brain-demo \
  --region=us-central1 \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=cloudbuild.audit.yaml \
  --substitutions=_TAG=adr-NNNN-shortname \
  .

# Roll the image out across the five jobs.
for job in asb-audit-sensitive-isolation asb-audit-sensitive-iam-drift \
           asb-audit-drafts-boundary asb-audit-bucket-iam-drift \
           asb-audit-cost-daily-check; do
  gcloud run jobs update $job \
    --project=agency-brain-demo --region=us-central1 \
    --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/asb-audit:adr-NNNN-shortname
done

# Force-fire one job to confirm the new code runs end-to-end.
gcloud run jobs execute asb-audit-drafts-boundary \
  --project=agency-brain-demo --region=us-central1 --wait
```

The Cloud Build SA `asb-cloud-build-sa` holds `roles/artifactregistry.writer`
at project scope (per `terraform/modules/foundation/iam.tf`); without it the
push step fails with `artifactregistry.repositories.uploadArtifacts denied`.

## First-run bootstrap (one-time)

After the first `terraform apply` deploys the five jobs, the baselines are
empty `{}` so every binding shows up as drift. To prime them:

```bash
# 1. Build and push the first asb-audit image (uses default _TAG=bootstrap).
gcloud builds submit \
  --project=agency-brain-demo \
  --region=us-central1 \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=cloudbuild.audit.yaml \
  .

# 2. Update each Cloud Run Job to point at the just-built image.
for job in asb-audit-sensitive-isolation asb-audit-sensitive-iam-drift \
           asb-audit-drafts-boundary asb-audit-bucket-iam-drift \
           asb-audit-cost-daily-check; do
  gcloud run jobs update $job \
    --project=agency-brain-demo --region=us-central1 \
    --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/asb-audit:bootstrap
done

# 3. Capture baselines locally and commit.
gcloud auth application-default login
python -m agency_brain.audit.hipaa_iam_drift --capture > \
  terraform/modules/security/expected/brain_iam_baseline.json
python -m agency_brain.audit.bucket_iam_drift --capture > \
  terraform/modules/security/expected/bucket_iam_baseline.json
git add terraform/modules/security/expected/
git commit -m "Capture initial brain IAM + bucket IAM baselines (post WS-F PR #2 deploy)"

# 4. Rebuild the image so the new baselines ship to the running jobs.
gcloud builds submit ...   # same command as step 1
```

## Tuning + silencing

Cadence too aggressive? Edit `local.audit_jobs[*].schedule` in
`terraform/modules/security/runtime_audits.tf` and re-apply.

False-positive on a specific finding? Two paths:
- Update the baseline (legitimate state).
- Add an explicit allowlist to the script (corner case in a recurring
  finding). For PR #2 there's no allowlist mechanism; if one is needed,
  add it as a follow-up PR with an ADR explaining the carve-out.

## Related documents

- [PRD.md §4 — Security Charter](../../PRD.md)
- [docs/acceptance/ws-f-security.md](../acceptance/ws-f-security.md)
- [docs/adr/0012-runtime-audit-architecture.md](../adr/0012-runtime-audit-architecture.md)
- [terraform/modules/security/expected/README.md](../../terraform/modules/security/expected/README.md)
