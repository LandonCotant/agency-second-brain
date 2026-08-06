# Runbook: Sensitive-account Isolation — End-to-End Verification

The unit tests in `tests/security/test_hipaa_isolation.py` prove the
mechanism is wired correctly: every Airtable request carries the
canonical `filterByFormula`, the Lookup fields are codified, and a
filtered-out record produces a `WRITE_TRUNCATE` payload that excludes it.
This runbook is the **live end-to-end** verification: prove that flipping
`HIPAA = true` on a real Airtable record removes the corresponding
`airtable_replica.*` rows within one sync cycle.

Required by `docs/acceptance/ws-b-data-pipeline.md` (functional checks).
the operator executes this against the production base before signing off the PR
and records the row counts in the PR description.

## Prerequisites

- `asb-airtable-sync` Cloud Run Job is deployed and the latest scheduler
  tick has succeeded (check Cloud Logging for `"sync run complete"`).
- `bq` CLI authenticated as a human admin with read access to
  `airtable_replica.*`.

## Procedure

```bash
PROJECT=agency-brain-demo
DATASET=airtable_replica
JOB=asb-airtable-sync
REGION=us-central1
```

### 1. Pick or create a test client

In Airtable's Brain base, find a low-stakes client to flip (or create a
throwaway one named `HIPAA-VERIFY-YYYYMMDD` with `HIPAA = false`). Add
one Project linked to it and one Task linked to that Project. Note the
three Airtable record IDs (visible in the URL when viewing the record).

```
CLIENT_ID=recXXXXXXXXXXXXXX
PROJECT_ID=recYYYYYYYYYYYYYY
TASK_ID=recZZZZZZZZZZZZZZ
```

### 2. Trigger a sync and confirm the rows are present

```bash
gcloud run jobs execute $JOB \
  --project=$PROJECT --region=$REGION --wait

bq query --project_id=$PROJECT --use_legacy_sql=false "
  SELECT 'clients' AS t, _airtable_record_id FROM \`$PROJECT.$DATASET.clients\`
   WHERE _airtable_record_id = '$CLIENT_ID'
  UNION ALL
  SELECT 'projects', _airtable_record_id FROM \`$PROJECT.$DATASET.projects\`
   WHERE _airtable_record_id = '$PROJECT_ID'
  UNION ALL
  SELECT 'tasks', _airtable_record_id FROM \`$PROJECT.$DATASET.tasks\`
   WHERE _airtable_record_id = '$TASK_ID'
"
```

Expect 3 rows. Record the result in the PR description as
"Pre-flip row counts: 3/3."

### 3. Flip HIPAA to true

In Airtable, set the test client's `HIPAA` checkbox to true. Save.

Verify in Airtable that `Projects.Client HIPAA` and `Tasks.Project HIPAA`
both recompute to `true` (Airtable Lookup fields update within a few
seconds — check the project and task records).

### 4. Trigger another sync and confirm the rows are gone

```bash
gcloud run jobs execute $JOB \
  --project=$PROJECT --region=$REGION --wait

# Re-run the same query. Expect zero rows.
bq query --project_id=$PROJECT --use_legacy_sql=false "
  SELECT 'clients' AS t, _airtable_record_id FROM \`$PROJECT.$DATASET.clients\`
   WHERE _airtable_record_id = '$CLIENT_ID'
  UNION ALL
  SELECT 'projects', _airtable_record_id FROM \`$PROJECT.$DATASET.projects\`
   WHERE _airtable_record_id = '$PROJECT_ID'
  UNION ALL
  SELECT 'tasks', _airtable_record_id FROM \`$PROJECT.$DATASET.tasks\`
   WHERE _airtable_record_id = '$TASK_ID'
"
```

Record "Post-flip row counts: 0/3" in the PR description.

### 5. Restore (only if you flipped a real client, not a throwaway)

```
# Set HIPAA = false again. Next sync re-adds the rows.
```

If you used a throwaway client, leave it as-is or delete it from Airtable;
the next sync will reflect the deletion.

## Failure modes

If post-flip row count > 0:

1. Check the most recent Cloud Run Job execution log for errors. A failed
   execution leaves the previous (pre-flip) snapshot in place.
2. Check the Airtable Lookup fields actually recomputed — open
   `Projects.Client HIPAA` for the linked Project; if it's still
   `false`, the link is broken or the Lookup field is misconfigured.
3. Manually inspect the `filterByFormula` in the most recent request: in
   Cloud Logging filter for `resource.type="cloud_run_job"
   resource.labels.job_name="asb-airtable-sync"` and search for
   `Bearer pat...` — the orchestrator logs the formula at INFO level
   (PII-free).
4. Open a P0: a HIPAA filter regression is the most serious failure mode
   in this system (PRD §4.1).

## Why this runbook exists when the unit test already passes

The unit test mocks the Airtable HTTP layer. It cannot prove that the
real Airtable base actually evaluates `NOT({Client HIPAA})` the way we
expect, that the Lookup fields are configured correctly in the live base,
or that the Cloud Run Job's network egress to `api.airtable.com` works at
all. This runbook closes those gaps.
