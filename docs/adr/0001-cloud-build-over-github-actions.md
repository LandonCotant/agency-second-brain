# ADR 0001 — Cloud Build over GitHub Actions for CI

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-A (Foundation)

## Context

The PRD (§3) mandates Cloud Build as the CI runner. We considered GitHub Actions as an alternative since the source repo lives on GitHub, but settled on Cloud Build for the following reasons.

## Decision

PR checks run on Cloud Build, triggered via the Cloud Build GitHub App.

## Rationale

- **Tighter GCP IAM integration.** Cloud Build runs as a GCP service account in the same project as the resources it inspects (`terraform plan`, IAM checks against live state). GitHub Actions would require a Workload Identity Federation pool plus an OIDC trust relationship — more moving parts to keep secure.
- **PRD §3 mandate.** The PRD lists Cloud Build explicitly. Deviating without a clear advantage burns trust in the PRD as a source of truth.
- **No long-lived secrets in CI.** Cloud Build's per-build SA is provisioned by the platform; nothing has to be stored in GitHub Secrets. This aligns with PRD §3 "Identity = Workload Identity Federation; no long-lived keys."
- **Cost.** Cloud Build's free tier (120 build-minutes/day) covers WS-A through WS-G PR volume.

## Consequences

- One-time manual step: connect the GitHub repo to Cloud Build via the Console (documented in `docs/runbooks/cloud_build_setup.md` once that runbook exists).
- A `cloudbuild.yaml` lives at the repo root. Branch protection on `main` requires this build to pass.
- The Cloud Build SA gets `roles/viewer` on `agency-brain-demo` and `roles/storage.objectViewer` on `asb-tfstate-prod` — minimum to read state for `terraform plan`. No write/admin roles. Apply happens locally by the human Owner until WS-E adds CD.

## Revisit if

- Cloud Build becomes a billing-line surprise (unlikely at our scale).
- We add a second trigger source (e.g., Linear webhooks) and the consolidation pressure flips toward Actions.
