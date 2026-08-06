# Runbook: Deploy the runtime jobs (resolve the 23-resource TF drift)

**Why this exists:** 23 resources are defined in TF code but were never applied to GCP. They are: the Airtable→BQ sync Cloud Run Job + scheduler (4 resources), the four runtime audit Cloud Run Jobs + their schedulers + BQ IAM bindings (18), and the HIPAA isolation alert policy (1). Until they're deployed, `terraform plan` is noisy and an unscoped `terraform apply` would bring all 23 online at once with broken images, immediate scheduler ticks, and a paging alert.

**This runbook walks through deploying them in three phases**, lowest blast radius first. Each phase is independent — you can stop after any one. After Phase 3, `terraform plan` is clean and the trap is gone.

**Time:** ~30 min (Phase 1) + 45 min (Phase 2) + 15 min (Phase 3) = ~1.5 hours total.

---

## Prerequisites (verify before starting)

Already confirmed at the time of writing:

- `gcloud auth list` → `owner@example.com` active
- ADC quota project = `agency-brain-demo`
- Secret Manager `airtable-pat-prod` exists with 1 enabled version
- `terraform/envs/prod/terraform.tfvars` has `airtable_base_id = "appXXXXXXXXXXXXXX"` and `region = "us-central1"`
- `terraform plan` shows exactly 23 resources to add, 0 to change, 0 to destroy
- All Artifact Registry repos exist: `asb-sync`, `asb-audit`
- All service accounts + custom IAM roles for the audit jobs are in TF state
- Both Dockerfiles (`Dockerfile`, `Dockerfile.audit`) build clean in CI

If any of those have changed, stop and reconcile first.

---

## Phase 1 — Deploy the Airtable sync (~30 min)

**What gets deployed (4 resources):**
- `module.data_pipeline.google_cloud_run_v2_job.tb_airtable_sync`
- `module.data_pipeline.google_cloud_run_v2_job_iam_member.scheduler_invoker`
- `module.data_pipeline.google_cloud_scheduler_job.tb_airtable_sync_15m`
- `module.data_pipeline.google_bigquery_dataset_iam_member.tb_sync_airtable_replica_editor`

**Effect:** Sync starts running every 15 min. Reads from Airtable Operations base (read-only PAT). Writes to `airtable_replica.*` (WRITE_TRUNCATE per cycle, so re-runs are no-ops). Unblocks Triage Agent (which reads the replica).

### Step 1.1 — Build & push the sync image

The container has to exist before the Cloud Run Job can execute. The TF references the `:bootstrap` tag, and `lifecycle.ignore_changes` lets future image tags roll forward without TF fighting them.

```bash
cd "/path/to/agency-second-brain"

gcloud builds submit \
  --tag=us-central1-docker.pkg.dev/agency-brain-demo/asb-sync/airtable-to-bq:bootstrap \
  --project=agency-brain-demo \
  .
```

The default `--tag` flag tells Cloud Build to use the `Dockerfile` at the build-context root, which is what we want. Expect ~3–5 min.

Verify the image landed:
```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/agency-brain-demo/asb-sync/airtable-to-bq \
  --project=agency-brain-demo
```

### Step 1.2 — Targeted apply

```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"

terraform apply \
  -target=module.data_pipeline.google_bigquery_dataset_iam_member.tb_sync_airtable_replica_editor \
  -target=module.data_pipeline.google_cloud_run_v2_job.tb_airtable_sync \
  -target=module.data_pipeline.google_cloud_run_v2_job_iam_member.scheduler_invoker \
  -target=module.data_pipeline.google_cloud_scheduler_job.tb_airtable_sync_15m
```

Review the plan (4 to add, 0 to change, 0 to destroy). Type `yes`.

### Step 1.3 — Manual smoke test before the scheduler fires

Don't wait for the next 15-min tick — trigger a run by hand so you can read the result interactively:

```bash
gcloud run jobs execute asb-airtable-sync \
  --region=us-central1 \
  --project=agency-brain-demo \
  --wait
```

Expect "Execution completed successfully." Tail logs if it failed:
```bash
gcloud run jobs executions list --job=asb-airtable-sync --region=us-central1 --project=agency-brain-demo --limit=1
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="asb-airtable-sync"' \
  --project=agency-brain-demo --limit=50 --format='value(textPayload,jsonPayload)'
```

### Step 1.4 — Verify replica populated

```bash
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  'SELECT COUNT(*) AS cnt FROM `agency-brain-demo.airtable_replica.service_catalog`'
# Expect: 7 (matches the seed rows in the Operations base)

bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  'SELECT table_name, row_count FROM `agency-brain-demo.airtable_replica.__TABLES__` ORDER BY table_name'
# Expect: 8 tables with the seed counts (clients=0, projects=0, tasks=0, goals=0, goal_scores=0, team=1, risk_profiles=10, service_catalog=7)
```

### Step 1.5 — Confirm the scheduler will fire on its own

```bash
gcloud scheduler jobs describe asb-airtable-sync-15m \
  --location=us-central1 --project=agency-brain-demo \
  --format="value(state,schedule,lastAttemptTime)"
# Expect: state=ENABLED, schedule="*/15 * * * *"
```

Wait up to 15 min, then re-run the count query — should be unchanged (WRITE_TRUNCATE means the replica matches Airtable; a no-op rewrite produces the same counts).

### Phase 1 rollback (if it goes sideways)

```bash
gcloud scheduler jobs pause asb-airtable-sync-15m --location=us-central1 --project=agency-brain-demo
# Stops new runs without destroying anything; you can investigate calmly.
```

To fully unwind:
```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"
terraform destroy \
  -target=module.data_pipeline.google_cloud_scheduler_job.tb_airtable_sync_15m \
  -target=module.data_pipeline.google_cloud_run_v2_job_iam_member.scheduler_invoker \
  -target=module.data_pipeline.google_cloud_run_v2_job.tb_airtable_sync \
  -target=module.data_pipeline.google_bigquery_dataset_iam_member.tb_sync_airtable_replica_editor
```
The PAT secret, the SAs, the AR repo, the Pub/Sub topics all stay (they're already deployed and orthogonal to this job).

---

## Phase 2 — Deploy the audit jobs (~45 min)

**What gets deployed (18 resources):**
- 4 × `google_cloud_run_v2_job.audit[*]` (one per check)
- 4 × `google_cloud_run_v2_job_iam_member.audit_invoker[*]`
- 4 × `google_cloud_scheduler_job.audit[*]`
- 4 × `google_bigquery_dataset_iam_member.audit_log_writer[*]` (BQ write on `agent_audit_log`)
- `google_bigquery_dataset_iam_member.hipaa_isolation_replica_viewer` (BQ read on `airtable_replica`)
- `google_bigquery_dataset_iam_member.hipaa_isolation_outputs_viewer` (BQ read on `agent_outputs`)

**Effect:** Four scheduled audits start running:

| Job | Schedule | What it checks |
|---|---|---|
| `asb-audit-sensitive-isolation` | hourly (`0 * * * *`) | PRD §4.1 layer 5 — no HIPAA-flagged data leaked into the Brain |
| `asb-audit-sensitive-iam-drift` | daily 06:00 UTC | PRD §4.1 layer 1 — Brain project IAM didn't drift from baseline |
| `asb-audit-drafts-boundary` | nightly 03:00 UTC | PRD §4.7 — no agent SA holds a forbidden role |
| `asb-audit-bucket-iam-drift` | daily 04:00 UTC | ADR 0005 compensating control — GCS bucket IAM didn't drift |

Each emits one row to `agent_audit_log.events` per run.

### Step 2.1 — Build & push the audit image

`Dockerfile.audit` is a non-default filename, so use a one-shot Cloud Build config rather than `--tag` shorthand:

```bash
cd "/path/to/agency-second-brain"

cat > /tmp/cloudbuild-audit-bootstrap.yaml <<'EOF'
steps:
  - name: gcr.io/cloud-builders/docker
    args:
      - build
      - --file=Dockerfile.audit
      - --tag=us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/asb-audit:bootstrap
      - .
images:
  - us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/asb-audit:bootstrap
EOF

gcloud builds submit \
  --config=/tmp/cloudbuild-audit-bootstrap.yaml \
  --project=agency-brain-demo \
  .
```

Verify:
```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/agency-brain-demo/asb-audit/asb-audit \
  --project=agency-brain-demo
```

### Step 2.2 — Targeted apply

```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"

terraform apply \
  -target=module.security.google_bigquery_dataset_iam_member.audit_log_writer \
  -target=module.security.google_bigquery_dataset_iam_member.hipaa_isolation_replica_viewer \
  -target=module.security.google_bigquery_dataset_iam_member.hipaa_isolation_outputs_viewer \
  -target=module.security.google_cloud_run_v2_job.audit \
  -target=module.security.google_cloud_run_v2_job_iam_member.audit_invoker \
  -target=module.security.google_cloud_scheduler_job.audit
```

The `for_each` resources expand inside each target, so 6 target flags create 18 resources. Plan should show "18 to add, 0 to change, 0 to destroy." Type `yes`.

### Step 2.3 — Smoke test each job individually

Each job runs a different script and exercises a different IAM surface, so test each before letting the schedulers loose. Start with `hipaa-isolation` since it's the most critical and the only one that touches multiple datasets:

```bash
for job in asb-audit-sensitive-isolation asb-audit-sensitive-iam-drift asb-audit-drafts-boundary asb-audit-bucket-iam-drift; do
  echo "=== $job ==="
  gcloud run jobs execute "$job" --region=us-central1 --project=agency-brain-demo --wait
  echo
done
```

If any fail, get the execution logs:
```bash
gcloud run jobs executions list --job=asb-audit-sensitive-isolation --region=us-central1 --project=agency-brain-demo --limit=1
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="asb-audit-sensitive-isolation"' \
  --project=agency-brain-demo --limit=50
```

### Step 2.4 — Verify the audit log has fresh rows

```bash
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  'SELECT agent_id, success, hipaa_guard_status, MAX(timestamp) AS last_seen
   FROM `agency-brain-demo.agent_audit_log.events`
   WHERE agent_id LIKE "audit-%"
   GROUP BY 1,2,3
   ORDER BY 1'
```

Expect 4 rows (one per audit job), all with `success=true` and `hipaa_guard_status=PASSED`.

### Step 2.5 — Pause the schedulers if you don't want them firing yet

The `hipaa-isolation` schedule fires at the top of every hour. If the deploy lands close to one of those times and you'd rather verify in daylight before letting it loose:

```bash
for job in asb-audit-sensitive-isolation-cron asb-audit-sensitive-iam-drift-cron asb-audit-drafts-boundary-cron asb-audit-bucket-iam-drift-cron; do
  gcloud scheduler jobs pause "$job" --location=us-central1 --project=agency-brain-demo
done

# Resume when ready:
for job in ...; do
  gcloud scheduler jobs resume "$job" --location=us-central1 --project=agency-brain-demo
done
```

### Phase 2 rollback

```bash
# Pause first to stop new runs:
for job in asb-audit-sensitive-isolation-cron asb-audit-sensitive-iam-drift-cron asb-audit-drafts-boundary-cron asb-audit-bucket-iam-drift-cron; do
  gcloud scheduler jobs pause "$job" --location=us-central1 --project=agency-brain-demo
done

# Then targeted destroy (reverse dependency order):
cd "/path/to/agency-second-brain/terraform/envs/prod"
terraform destroy \
  -target=module.security.google_cloud_scheduler_job.audit \
  -target=module.security.google_cloud_run_v2_job_iam_member.audit_invoker \
  -target=module.security.google_cloud_run_v2_job.audit \
  -target=module.security.google_bigquery_dataset_iam_member.audit_log_writer \
  -target=module.security.google_bigquery_dataset_iam_member.hipaa_isolation_replica_viewer \
  -target=module.security.google_bigquery_dataset_iam_member.hipaa_isolation_outputs_viewer

# Optional: clean error rows out of the audit log
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  'DELETE FROM `agency-brain-demo.agent_audit_log.events` WHERE agent_id LIKE "audit-%" AND success = false'
```

---

## Phase 3 — Deploy the HIPAA isolation alert policy (~15 min)

**What gets deployed (1 resource):**
- `module.observability.google_monitoring_alert_policy.hipaa_isolation_breach`

**Effect:** When `asb-hipaa-guard-tripped` log-based metric registers any nonzero count over 5 minutes (i.e., any log line matches `HIPAA_GUARD_TRIPPED`), the alert fires and posts to the `Brain alerts` Google Chat space (notification channel already deployed).

**Prerequisite:** Phase 2 should be done, so the hourly `asb-audit-sensitive-isolation` job is live and feeding the metric. (The alert can deploy without Phase 2; it just won't have anything to alert on until something emits `HIPAA_GUARD_TRIPPED`.)

### Step 3.1 — Targeted apply

```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"

terraform apply \
  -target=module.observability.google_monitoring_alert_policy.hipaa_isolation_breach
```

Plan: 1 to add, 0 to change, 0 to destroy.

### Step 3.2 — Smoke test (verify the alert fires end-to-end)

The policy has a 5-min aggregation window, so allow ~5–7 min between writing the test log and seeing the Chat notification.

```bash
gcloud logging write asb-hipaa-test \
  '{"event":"HIPAA_GUARD_TRIPPED","note":"smoke test from runbook deploy_runtime_jobs phase 3"}' \
  --severity=ERROR --payload-type=json --project=agency-brain-demo
```

What to verify:
1. Within ~1 min: the log entry shows up in Cloud Logging
2. Within ~5–7 min: the alert fires — visible in Cloud Monitoring → Alerting
3. Within ~5–7 min: a message lands in the `Brain alerts` Google Chat space (posted by the Cloud Monitoring app)
4. Email channel is intentionally NOT on this alert (PRD §8.2: Chat-only for the P0 to keep it distinct from lower-severity email alerts)

If the Chat post doesn't arrive, check Cloud Monitoring → Notification Channels for delivery errors on `chat_brain_alerts`.

### Step 3.3 — Resolve the test incident in the console

Go to Cloud Monitoring → Alerting → Incidents and resolve the `HIPAA isolation breach (P0)` incident triggered by the smoke test.

### Phase 3 rollback

```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"
terraform destroy -target=module.observability.google_monitoring_alert_policy.hipaa_isolation_breach
```

The log-based metric and notification channels stay (they're already deployed and useful regardless).

---

## After all three phases

### Verify TF plan is clean

```bash
cd "/path/to/agency-second-brain/terraform/envs/prod"
terraform plan
# Expect: "No changes. Your infrastructure matches the configuration."
```

### Update docs/PRODUCTION_STATE.md

After a deploy, update `docs/PRODUCTION_STATE.md`:
- Bump the `Last verified:` date stamp at the top
- Edit the affected module's row in the deployment table
- Update the schedulers table if you added or removed a cron

### Verify the audit log shows steady-state rows

After 24 hours, the audit log should show:
- ~24 rows from `audit-hipaa-isolation` (hourly)
- 1 row from each of `audit-hipaa-iam-drift`, `audit-drafts-boundary`, `audit-bucket-iam-drift` (daily)
- Plus any `airtable_sync` rows if the sync emits to the audit log (check the sync's Python source)

```bash
bq query --project_id=agency-brain-demo --use_legacy_sql=false \
  'SELECT agent_id, COUNT(*) AS runs, MAX(timestamp) AS last_run, COUNTIF(success) AS successes
   FROM `agency-brain-demo.agent_audit_log.events`
   WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
   GROUP BY 1
   ORDER BY 1'
```

If any audit job shows `successes < runs`, look at its error column — the script may need a fix or its baseline JSON in `terraform/modules/security/expected/` may need refreshing.

---

## Troubleshooting

**Cloud Run Job execution fails with `Image not found` or `manifest unknown`**
The image push didn't land or pushed to the wrong tag. Re-run Step 1.1 / 2.1 and verify with `gcloud artifacts docker images list ...`.

**Cloud Run Job execution fails with permission errors**
The TF dependencies on IAM bindings (`depends_on = [google_*_iam_member.*]`) ensure the binding is created before the job, but eventual consistency in IAM can take ~30 sec. Re-run the execution after a minute. If it still fails, double-check the SA has the expected role:
```bash
gcloud projects get-iam-policy agency-brain-demo --flatten="bindings[].members" \
  --filter="bindings.members:asb-sync-airtable-sa@*" --format="value(bindings.role)"
```

**Cloud Run Job times out**
- Sync default timeout is 900s (15 min). Should be plenty for 8 small Airtable tables.
- Audit jobs default to 180s–300s. If a check is slow, the script needs profiling.

**Alert never fires after the smoke test log write**
- Confirm the log write succeeded (`gcloud logging read 'textPayload:"smoke test from runbook"' --limit=1`)
- Confirm the log-based metric is incrementing (Cloud Monitoring → Metrics Explorer → `logging.googleapis.com/user/asb-hipaa-guard-tripped`)
- The 5-min aggregation window means the first fire can be up to 7 min after the log entry

**`terraform plan` shows unexpected drift after deploy**
The `lifecycle.ignore_changes` on `containers[0].image` should keep TF from fighting image-tag rollouts. If you see image drift, something is changing the image tag *outside* the cloudbuild rollout path — investigate before applying.
