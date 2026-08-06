#!/usr/bin/env bash
# One-shot bootstrap for the Terraform GCS state backend.
#
# Why: terraform/envs/prod/backend.tf points at gs://asb-tfstate-prod, but on
# first apply that bucket doesn't exist yet — and we can't store the state of
# its own creation in itself. So we create the bucket out-of-band here, then
# `terraform init` connects to it.
#
# After this runs once, the bucket itself can be imported into TF state for
# drift detection (see docs/adr/0003-tfstate-bucket-bootstrap.md).
#
# Idempotent: re-running is safe — `gcloud storage buckets create` returns
# nonzero on existing bucket, which we tolerate.
#
# Required env vars:
#   GCP_PROJECT_ID  — defaults to agency-brain-demo
#   GCP_REGION      — defaults to us-central1
#   GCP_BILLING     — billing account to attach (only used if project doesn't exist)
#   GCP_ORG_ID      — org id (only used if project doesn't exist)
#
# This script does the *minimum* to unblock `terraform init`. It does NOT
# create the brain project itself — Terraform does that on first apply.
# But the state bucket has to live somewhere, so we put it in a tiny
# bootstrap project that Terraform later imports and manages.
#
# Strategy: create a separate `asb-tfstate-bootstrap` project just for the
# state bucket. That keeps the state backend isolated from the workload
# projects (so a destructive `terraform destroy` on prod can't ever delete
# its own state).

set -euo pipefail

BOOTSTRAP_PROJECT="${BOOTSTRAP_PROJECT:-asb-tfstate-bootstrap}"
STATE_BUCKET="${STATE_BUCKET:-asb-tfstate-prod}"
REGION="${GCP_REGION:-us-central1}"
BILLING="${GCP_BILLING:-}"
ORG_ID="${GCP_ORG_ID:-}"

if [[ -z "${BILLING}" || -z "${ORG_ID}" ]]; then
  echo "ERROR: set GCP_BILLING and GCP_ORG_ID environment variables." >&2
  echo "  export GCP_BILLING=000000-000000-000000" >&2
  echo "  export GCP_ORG_ID=000000000000" >&2
  exit 2
fi

echo "==> Bootstrap project: ${BOOTSTRAP_PROJECT}"
if ! gcloud projects describe "${BOOTSTRAP_PROJECT}" >/dev/null 2>&1; then
  gcloud projects create "${BOOTSTRAP_PROJECT}" \
    --organization="${ORG_ID}" \
    --name="TB Terraform State Bootstrap"
  gcloud beta billing projects link "${BOOTSTRAP_PROJECT}" \
    --billing-account="${BILLING}"
else
  echo "    already exists — skipping creation"
fi

echo "==> Enabling storage.googleapis.com on ${BOOTSTRAP_PROJECT}"
gcloud services enable storage.googleapis.com --project="${BOOTSTRAP_PROJECT}"

echo "==> Creating state bucket: gs://${STATE_BUCKET}"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${BOOTSTRAP_PROJECT}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
  gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
else
  echo "    already exists — skipping creation"
fi

echo "==> Lifecycle: delete noncurrent state versions older than 90 days"
cat >/tmp/asb-tfstate-lifecycle.json <<'EOF'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"daysSinceNoncurrentTime": 90, "isLive": false}
      }
    ]
  }
}
EOF
gcloud storage buckets update "gs://${STATE_BUCKET}" \
  --lifecycle-file=/tmp/asb-tfstate-lifecycle.json
rm -f /tmp/asb-tfstate-lifecycle.json

echo "==> Done. Run: cd terraform/envs/prod && terraform init"
