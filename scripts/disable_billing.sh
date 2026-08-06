#!/usr/bin/env bash
# Manual kill switch — detaches the billing account from a project, halting
# all paid resources within minutes. ADR 0030: deliberately a manual script
# rather than auto-shutoff because granting any service account
# billing.resourceAssociations.delete on the billing account expands blast
# radius across the 5 projects on that account for marginal latency benefit.
#
# Usage:
#   bash scripts/disable_billing.sh <PROJECT_ID>
#
# Effects:
#   - Within ~5 min: every paid resource in the project stops billing.
#     Cloud Run Jobs / Schedulers / Cloud Functions / Reasoning Engines /
#     BQ jobs / Vertex AI / Secret Manager — all halt.
#   - State (BQ tables, GCS objects, Pub/Sub messages) is RETAINED.
#   - Pre-existing data is preserved up to 30 days; after that, GCP may
#     start deleting un-billable resources.
#
# Recovery: re-link the billing account with the command this script
# prints on completion. Schedulers and Cloud Run Jobs resume on their next
# tick automatically.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <PROJECT_ID>" >&2
  echo "" >&2
  echo "  Detaches the billing account from <PROJECT_ID>, halting all paid resources." >&2
  echo "  ADR 0030 — manual kill switch invoked when daily spend alert fires." >&2
  exit 64
fi

PROJECT_ID="$1"
BILLING_ACCOUNT="000000-000000-000000"  # the Agency billing account

# Show current state so the operator confirms they're hitting the right project.
echo "=== Current billing info for ${PROJECT_ID} ==="
gcloud beta billing projects describe "${PROJECT_ID}" --format="yaml(billingAccountName,billingEnabled,projectId)"
echo ""

read -r -p "Detach billing account from ${PROJECT_ID}? This stops all paid resources. [y/N] " confirm
if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
  echo "aborted." >&2
  exit 1
fi

echo ""
echo "=== Detaching ==="
gcloud beta billing projects unlink "${PROJECT_ID}"
echo ""

echo "=== Done. Verifying ==="
gcloud beta billing projects describe "${PROJECT_ID}" --format="yaml(billingAccountName,billingEnabled,projectId)"
echo ""

echo "=== TO RE-ENABLE (when ready): ==="
echo ""
echo "  gcloud beta billing projects link ${PROJECT_ID} --billing-account=${BILLING_ACCOUNT}"
echo ""
echo "After re-linking, schedulers + Cloud Run Jobs resume on their next tick."
