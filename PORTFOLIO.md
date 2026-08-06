# Portfolio copy notes

This repository is a **sanitized public copy** of a private production system.

## What was changed

- Rebranded to **Agency Second Brain** (`agency_brain` Python package)
- Replaced live GCP project IDs, org/billing IDs, Airtable IDs, Drive folder IDs,
  Chat space IDs, and operator emails with placeholders
- Removed live `terraform.tfvars`, production state snapshots, and operator-only
  runbooks (Workspace DWD click-paths, PAT rotation, etc.)
- Case study generalized; client-specific sales pitches removed
- Product language uses **sensitive / regulated-account isolation**; some code
  identifiers retain historical `hipaa_*` names so filters and tests stay coherent

## What was preserved

- Agent source, Terraform modules, ADRs, security audits, and unit/security tests
- Redacted Airtable schema shapes
- Drafts-only and least-privilege design invariants

## Do not

- Assume placeholders are deployable against a real org without your own IDs
- Treat IAM baselines as a map of anyone's live cloud IAM
