# ADR 0003 — One-shot bootstrap for the Terraform state bucket

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-A (Foundation)

## Context

The Terraform configuration in `terraform/envs/prod/` uses a GCS backend (`gs://asb-tfstate-prod`). On first apply, that bucket doesn't exist yet — and `terraform init` cannot proceed without a backend it can read. The bucket also can't be managed by the same Terraform that depends on its own state.

## Decision

A one-shot bash script `scripts/bootstrap_tfstate.sh` creates a separate `asb-tfstate-bootstrap` project, enables Cloud Storage on it, and creates the state bucket with versioning, uniform bucket-level access, and a 90-day noncurrent-version lifecycle rule. The script is idempotent — re-running is safe.

## Rationale

- **Bucket lives in its own project.** Putting the state bucket in `agency-brain-demo` would mean a destructive `terraform destroy` could orphan its own state. Putting it in `asb-tfstate-bootstrap` isolates it.
- **Bash script over Terraform.** A separate Terraform module to manage the state bucket is over-engineering for a one-time bootstrap. A bash script with `set -euo pipefail` is auditable in 30 lines.
- **Idempotent.** Running on an already-bootstrapped environment is a no-op.

## Consequences

- The state bucket is **not** managed in Terraform after bootstrap. Drift on the bucket itself (e.g., versioning accidentally disabled) won't be caught by `terraform plan`. Acceptable trade-off; offset by the bucket living in a dedicated project nobody else touches.
- Onboarding a second engineer requires they have the org-level permissions to *read* the existing bucket, not to create one — bootstrap runs once per environment.
- If the org is recreated or migrated, `bootstrap_tfstate.sh` must be re-run before any `terraform init`.

## Revisit if

- We add a second environment (e.g., `staging`). The script would need to take env name as an arg; currently hardcoded to `prod`.
- We move to Terraform Cloud / HCP Terraform, which manages state externally and removes the chicken-and-egg.
