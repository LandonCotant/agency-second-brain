# ADR 0004 — Project-scope org policies and audit sinks (deviation from PRD §4.1 / §4.6)

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-A (Foundation)

## Context

PRD §4.1 layer 1 calls for `iam.allowedPolicyMemberDomains` and related org policies "at the org level." PRD §4.6 calls for an aggregated org-level Cloud Audit Logs sink with `include_children = true`.

When WS-A actually inspected the org, we found ~12 pre-existing active projects under `example.com` (org ID 000000000000):

- `agency-mlops-hipaa` — the HIPAA-flagged project the PRD §4.1 references as "the project the Brain must never touch"
- `agency-client-a-prod-998877` — client production workload
- `agency-mlops-demo-prod`, `agency-mlops-dev` — operational MLOps projects
- `agency-file-upload-portal`, `gen-lang-client-*` — agency tooling
- `secondbrain-493921`, `bq-test-project-474922`, `ai-geo-tracker` — misc

These projects pre-date the Brain build and have unknown IAM bindings, possibly including:
- External-customer service accounts (vendor integrations, agency clients)
- Workflows that create SA keys for non-GCP integrations

Applying `iam.allowedPolicyMemberDomains` and `iam.disableServiceAccountKeyCreation` at the **org level** would retroactively constrain all of these — likely breaking real workflows.

## Decision

WS-A applies the four constraints (`iam.allowedPolicyMemberDomains`, `iam.disableServiceAccountKeyCreation`, `storage.uniformBucketLevelAccess`, `compute.requireOsLogin`) **at the project level only**, on `agency-brain-demo` and `asb-audit-logs-prod`.

WS-A creates a **project-level Cloud Audit Logs sink** on each Brain project, routing to the audit bucket — instead of an org-level aggregated sink with `include_children = true`.

## Rationale

- **Same Brain-side guarantee.** IAM bindings are project-scoped; an org policy at project scope is equivalent for the Brain itself. The HIPAA isolation goal — "the Brain's service accounts have no IAM grants in the HIPAA project" — is enforced by the *absence* of bindings, not by an org-wide constraint.
- **Zero blast radius on existing projects.** Agency client work, the HIPAA project, and misc tooling are unaffected. They retain their own existing IAM model.
- **Audit clarity.** The Brain audit bucket contains only Brain-related logs. Investigating an event doesn't require filtering out unrelated agency-client noise.
- **Reversible.** If a security review later argues for org-wide enforcement, promoting these from project to org scope is a small, deliberate PR.

## Consequences

- New projects under the org (created by anyone, for any purpose) **do not inherit** these constraints. WS-F should add a daily org-policy-coverage check that alerts if a new project under example.com hosts Brain-related resources without matching policies.
- The §4.1 layer 5 "continuous audit" script (`audit/hipaa_isolation_check.py`, owned by WS-F) gains importance — it's the runtime verification that compensates for not having org-wide IAM lockdown.
- The audit bucket only receives logs from the two Brain projects. Org-level admin events (e.g., billing changes, IAM at the org node) are NOT captured here. WS-F should evaluate whether to add a separate org sink for org-node events specifically (a much smaller log volume than `include_children = true`).

## Revisit if

- The agency / HIPAA workloads are migrated out of this org (then org-wide enforcement becomes free).
- A security review escalates the requirement.
- The Brain becomes a multi-tenant product (PRD §17), at which point org-wide enforcement is the only reasonable posture.
