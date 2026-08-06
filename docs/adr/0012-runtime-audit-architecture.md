# ADR 0012 — Runtime audit architecture

**Status:** Accepted
**Date:** 2026-04-28
**Workstream:** WS-F (Security & Compliance)
**Related:** PRD §4.1, §4.7, §4.8; ADR 0005 (default Cloud Audit Logs);
ADR 0006 (audit log streaming); ADR 0010 (Cloud Run Job for sync — same
hosting pattern reused here).

## Context

WS-F PR #1 hardened the three **PR-time** security gates
(`scripts/{least_privilege,hipaa_filter,model_armor}_check.py`). Those run
on every PR but cannot detect drift after merge. PRD §4.1 layer 5, §4.7,
and ADR 0005's compensating control require **runtime** verification of
the security charter — periodic jobs that re-prove the invariants against
the live state.

WS-F PR #2 ships four runtime audit scripts. This ADR captures the three
non-obvious design choices made in that PR. The Cloud Run Job hosting
pattern is unchanged from ADR 0010; this ADR does not re-litigate it.

## Decision 1 — Baselines as checked-in JSON, not BQ snapshots

Two of the four scripts (`hipaa_iam_drift`, `bucket_iam_drift`) compare
live state against an expected-state baseline. Three options were
considered:

| Option | Pros | Cons |
|---|---|---|
| **Checked-in JSON in repo** ✅ | Drift is reviewable in a PR diff. Operator updates baseline alongside the TF that caused the change. Free durability via git. No new write paths for the audit SA. | A repo edit is required to bless legitimate IAM changes — the friction is the feature. |
| Latest snapshot in a BQ table | No PR friction. Audit SA writes both "expected" and "actual" rows. | The audit SA needs **write** access to its own expected-state — a self-modifying check that defeats the purpose. Also: history is opaque without a TF/git anchor. |
| Terraform-derived (read state directly) | Single source of truth — `terraform plan` IS the diff. | Audit can't catch out-of-band TF drift (someone applied a console change without TF). Requires giving the audit SA TF state read access, which contradicts least-privilege. |

**Decision:** checked-in JSON. The friction of "update the baseline JSON
in the same PR as the IAM change" is desirable — it forces every IAM
change to be reviewable in two places. Files live at
`terraform/modules/security/expected/` rather than under `src/` because
they describe **infra** state, not application logic; co-locating with TF
makes the "TF apply changed bindings → update baseline" workflow a
one-directory edit.

## Decision 2 — Per-script SAs, not a shared audit SA

Per PRD §4.2 ("each agent, sync flow, and routing flow gets its own SA
with a custom IAM role"), every distinct workload gets its own SA. The
four audit scripts have **genuinely disjoint** permission needs:

| Script | Permission set |
|---|---|
| `hipaa_isolation_check` | BigQuery: jobs.create + dataset reads on `airtable_replica` + `agent_outputs` |
| `hipaa_iam_drift` | Cloud Resource Manager: `projects.getIamPolicy` |
| `drafts_boundary_check` | Cloud Resource Manager: `projects.getIamPolicy` |
| `bucket_iam_drift` | Cloud Storage: `buckets.list` + `buckets.getIamPolicy` |

A shared SA would force the union (BQ reads + project IAM read + storage
list/IAM read) — exactly what PRD §4.2 forbids. The bookkeeping cost of
four SAs is mitigated by `for_each` over `local.audit_jobs` in Terraform;
the security cost of consolidation is real.

The **scheduler invoker SA** is a different concern (it only needs
`run.invoker` on the four jobs) and a single shared invoker is fine —
compromising it grants invoke on these four jobs only, scoped via per-job
IAM bindings.

## Decision 3 — No cross-project IAM read for `hipaa_iam_drift`

PRD §4.1 layer 1 says: "The Brain's service accounts have no IAM grants
in [the HIPAA project]. Verified by an org-policy
`iam.allowedPolicyMemberDomains` constraint and a daily
`audit/hipaa_iam_drift.py` script."

The strict reading would have `hipaa_iam_drift` read IAM on `agency-hipaa-*`
projects directly. Doing so requires granting the audit SA
`resourcemanager.projects.getIamPolicy` on a peer project, which:

1. Crosses the HIPAA project boundary the SA topology was designed to
   isolate. The Brain SA having any grant in the HIPAA project would
   itself be a §4.1 violation.
2. Requires either (a) provisioning an IAM grant on the peer project
   (unattractive — see (1)), or (b) elevating the audit SA to
   org-level read via Cloud Asset Inventory (heavyweight for a 2-person
   tool).

**Decision:** PR #2's `hipaa_iam_drift` checks the **brain project's
own** IAM for drift. The cross-project assertion is covered by a
quarterly manual `gcloud asset search-all-iam-policies` task, documented
in `docs/runbooks/runtime_audit_response.md`. This is consistent with the
"pragmatic security over PRD-prescribed defense-in-depth when marginal
risk is low" feedback memory: the org-policy constraint
(`iam.allowedPolicyMemberDomains`) already blocks external members; an
out-of-band grant of a Brain SA to the HIPAA project would require an
internal admin acting deliberately, which the quarterly review catches
without paying the standing-permission cost.

If/when the Brain becomes multi-tenant or HIPAA exposure widens, revisit
by either (a) standing up Cloud Asset Inventory at folder level for the
audit SA, or (b) running the cross-project check from a separate SA with
resource-scoped access to the peer project.

## Consequences

- Adding a fifth runtime audit script adds one entry to
  `local.audit_jobs` in `terraform/modules/security/runtime_audits.tf`,
  one SA + one custom role + one Cloud Run Job + one Scheduler trigger.
  Pattern is uniform.
- Every legitimate IAM/bucket change requires a paired baseline-JSON
  update in the same PR. This is the desired friction.
- The cross-project HIPAA assertion is a quarterly chore rather than a
  daily automated check. Acceptable for the 2-person operating posture;
  flag for re-evaluation when scope grows.
- Container image is shared across all four jobs (`asb-audit:bootstrap`
  base, per-job `args` override). Adding a script doesn't multiply build
  artifacts.
