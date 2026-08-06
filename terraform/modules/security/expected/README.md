# Runtime audit baselines

The two JSON files in this directory are **expected-state snapshots** that
the WS-F PR #2 runtime audit scripts diff against the live state on every
scheduled run.

| File | Captured by | Compared against |
|---|---|---|
| `brain_iam_baseline.json` | `python -m agency_brain.audit.hipaa_iam_drift --capture` | live `cloudresourcemanager.projects.getIamPolicy` on `agency-brain-demo` |
| `bucket_iam_baseline.json` | `python -m agency_brain.audit.bucket_iam_drift --capture` | live IAM on every GCS bucket in the brain project |

## When to update

Every time the live IAM legitimately changes (new SA, role swap, new
bucket), update the corresponding baseline file in the **same PR** as the
infra change. The diff in `terraform plan` and the diff in this file should
move together — that's the whole reason these baselines live alongside the
Terraform module rather than under `src/`.

## How to update

```bash
# Authenticate as a human admin with project IAM read.
gcloud auth application-default login

# Capture the live state.
python -m agency_brain.audit.hipaa_iam_drift --capture > \
    terraform/modules/security/expected/brain_iam_baseline.json

python -m agency_brain.audit.bucket_iam_drift --capture > \
    terraform/modules/security/expected/bucket_iam_baseline.json

# Eyeball the diff. Anything unexpected is the signal — investigate before committing.
git diff terraform/modules/security/expected/
```

## First-run bootstrap

On first deploy, both files start as empty `{}` so every binding shows up
as drift. Run the capture commands above, eyeball the JSON, commit, and
the next scheduled run will pass cleanly.

See [`docs/runbooks/runtime_audit_response.md`](../../../../docs/runbooks/runtime_audit_response.md)
for the full operator playbook.
