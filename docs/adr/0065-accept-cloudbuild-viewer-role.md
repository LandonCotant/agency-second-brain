# ADR 0065 — Accept `roles/viewer` on the Cloud Build PR-checks SA

**Status:** Accepted
**Date:** 2026-06-10
**Workstream:** WS-F (security) — code-review follow-up

## Context

The 2026-06-10 codebase review flagged `roles/viewer` granted project-wide
to `asb-cloud-build-sa` (`terraform/modules/foundation/iam.tf`, resource
`cb_viewer`) as a High finding: a predefined broad role on a service
account, and one the PR-gate `scripts/least_privilege_check.py` does not
flag (its forbidden set is `roles/owner` / `roles/editor` / any
`*admin*` role — `roles/viewer` is intentionally not in it).

The grant exists because the PR-checks pipeline (`cloudbuild.yaml`) runs
`terraform plan` against live project state, which requires read access
across nearly every resource type in the project. The SA holds no
write/admin role: `apply` happens locally after merge (PRD §4.2 / WS-A
scope). Its other grants are `storage.objectViewer` on the tfstate bucket,
`logging.logWriter`, and `artifactregistry.writer`.

The review's suggested fix was to replace `roles/viewer` with a narrow
custom role scoped to exactly what `terraform plan` reads.

## Decision

**Keep `roles/viewer`. Do not narrow it.** Document the acceptance here.

### Why (security-vs-cost, per `feedback_security_vs_cost.md`)

**Marginal risk of keeping it is low:**

- `roles/viewer` is read-only. It grants `*.get` / `*.list` on resource
  metadata — it does **not** grant `secretmanager.versions.access` (secret
  *values*); only `secrets.get`/`list` (names + metadata). No data-plane
  read of BQ table *contents* beyond what `bigquery.dataViewer`-style roles
  give (viewer includes `bigquery.tables.get`/`list` metadata, not
  `tables.getData` on arbitrary tables by default for the basic viewer —
  and the replica carries no HIPAA rows anyway, ADR 0020).
- `asb-cloud-build-sa` is already a top-trust identity by design: it builds
  and pushes **every** container image (`artifactregistry.writer`,
  project-scope per the `cb_artifact_writer` rationale) and reads tfstate.
  An attacker who compromises it owns the image supply chain — a strictly
  larger capability than reading dataset/job/secret *names*. Narrowing the
  read role does not meaningfully shrink the blast radius of a compromised
  build SA; the supply-chain grant is the thing that matters, and that one
  is irreducible for a build SA.

**Marginal cost of narrowing is real and high:**

- `terraform plan` reads the full resource graph: BQ datasets/tables, Cloud
  Run jobs + services, Cloud Scheduler, Pub/Sub, Secret Manager metadata,
  project IAM, GCS, Dataplex aspect types, Artifact Registry, Model Armor,
  org-policy state, and the GCP-managed service-agent bindings. A custom
  role would need dozens of `*.get`/`*.list` permissions.
- It is a standing maintenance burden: every new resource *type* added to
  the Terraform (this project adds them regularly) would require updating
  the custom role, or `terraform plan` 403s and **blocks every PR** until
  someone diagnoses the missing permission. That failure mode is
  high-friction and easy to hit.
- This is precisely the "enterprise ceremony for a 2-person tool" the user
  preference warns against: real cost, low risk reduction.

### Guardrails that remain

- No write/admin/owner/editor role on the SA; `least_privilege_check.py`
  continues to enforce the forbidden set on every PR.
- `apply` is human-run locally post-merge; the SA cannot mutate prod.
- The IAM drift audit (`asb-audit-sensitive-iam-drift`, re-enabled per ADR 0058
  / 0064) baselines this exact binding — any *change* to the build SA's
  roles (e.g. someone adding `editor`) trips `SECURITY_DRIFT`.

## Consequences

**Positive**
- The review finding is resolved as a deliberate, documented decision
  rather than left ambiguous. CI stays robust against new resource types.

**Negative / accepted**
- A compromised build SA can enumerate project resource metadata (names,
  shapes, IAM bindings, secret names — not secret values). Accepted: the
  same compromise already grants image-push, the dominant risk.

## Revisit if

- A CD pipeline (WS-E) ever gives the build SA an `apply`/write path — at
  that point the trust posture changes and the read surface should be
  re-scoped alongside it in that ADR.
- The project ever ingests HIPAA data into BQ (currently deferred,
  `project_hipaa_deferred`) — viewer's BQ metadata reach should be
  re-evaluated against the isolation boundary then.

## References

- PRD §4.2 (no predefined high-privilege roles on SAs), §4.8 (PR gate)
- `feedback_security_vs_cost.md` (pragmatic-security user preference)
- ADR 0018 (legacy Compute/Cloud Build default SA disabled — why this
  custom SA exists)
- ADR 0058 / 0064 (IAM drift audit that baselines this binding)
- `terraform/modules/foundation/iam.tf` (`cb_viewer` resource)
- `scripts/least_privilege_check.py` (the gate; `roles/viewer` is
  intentionally outside its forbidden set)
