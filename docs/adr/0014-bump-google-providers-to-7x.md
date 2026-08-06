# 0014. Bump Google Terraform providers from 5.30 → 7.30

**Status:** Accepted
**Date:** 2026-04-28
**Workstream:** WS-G1 (Triage Agent)

## Context

WS-G1's PR 4 needs to deploy the Triage Agent as a Vertex AI Reasoning Engine per PRD §6.3. The Terraform resource for that, `google_vertex_ai_reasoning_engine`, is only available in the `google-beta` provider — and only since approximately mid-to-late 2025.

Our prod environment was pinned at `~> 5.30` for both `hashicorp/google` and `hashicorp/google-beta`, set during WS-A foundation work. The `5.30` schema does not include `google_vertex_ai_reasoning_engine`. We confirmed via `terraform providers schema -json` that only the IAM resources for it (`google_vertex_ai_reasoning_engine_iam_*`) and a query data source exist there; the resource itself is missing.

Without the resource, the alternatives were:
1. Bump providers to a version that includes it.
2. Deploy the Reasoning Engine outside of Terraform via `vertexai.preview.reasoning_engines.ReasoningEngine.create()` from a Python script. Trades TF state coverage for unfamiliar tooling.
3. Deviate from PRD §6.3 and host Triage as a Cloud Run Job (matching the sync + 4 audit jobs pattern). Pragmatic and proven, but means the agent doesn't get `runtimeRevisions` for prompt rollback.

## Decision

Bump both `hashicorp/google` and `hashicorp/google-beta` from `~> 5.30` to `~> 7.30` in:

- `terraform/envs/prod/versions.tf`
- `terraform/modules/{foundation,agent_runtime,data_pipeline,security,observability}/versions.tf` (or equivalent `terraform { ... }` block in `main.tf` for the `foundation` module)

Apply the upgrade as its own PR (this one) — separate from the Reasoning Engine deployment work — so the upgrade's blast radius is auditable in isolation.

## Rationale

The 5.30 → 7.30 jump crosses **two major versions** (5 → 6 → 7), which sounds risky. We mitigated by reading both major-version CHANGELOGs in advance and identifying the candidate breaking changes that could affect the project:

| 5 → 6 | Affects us? |
|---|---|
| `provider`: auto-add `goog-terraform-provisioned: true` label | Theoretically every labeled resource — turned out to be silently absorbed (no plan diff). |
| `resourcemanager`: `google_project.deletion_policy` default → `PREVENT` | **Yes** — exactly the one diff we saw. |
| `cloudrunv2`: `containers.env` retyped from ARRAY → SET | Not affected — our env declarations are order-independent. |
| `bigqueryreservation`, `compute`, `composer`, `redis`, `vpcaccess`, etc. | Not used in this project. |

| 6 → 7 | Affects us? |
|---|---|
| `bigquery`: removed default of `view.use_legacy_sql` | Our materialized view doesn't set this; default behaviour preserved. |
| `vertexai`: removed `enable_secure_private_service_connect` on `google_vertex_ai_endpoint` | Not used. |
| `cloudfunctions2`, `apigee`, `tpu`, `notebooks`, `beyondcorp` removals | Not used. |
| `compute`: various retypings | Not used. |

The actual `terraform plan` after `terraform init -upgrade` was **0 to add, 1 to change, 0 to destroy** — only `google_project.brain.deletion_policy` going `"DELETE" → "PREVENT"`. Applying that change *strengthens* prod-safety: a future accidental `terraform destroy` is now blocked unless someone explicitly sets `deletion_policy = "DELETE"` first. Net beneficial.

Tests pass (155). `terraform plan` returns "No changes" after the apply.

## Consequences

**Positive:**
- `google_vertex_ai_reasoning_engine` becomes deployable from TF in PR 4b, keeping us PRD §6.3-compliant on the Triage Agent host.
- Project is now soft-locked against accidental destruction (new default, applied via the diff).
- Two years of provider bug fixes and new resources are now available. Future workstreams (Knowledge Catalog, Memory Bank, etc.) benefit.

**Negative:**
- Future module additions must be checked against the 7.x schema. (No bigger than the 5.x case — same workflow.)
- The `hashicorp/google-beta` resources we now use (`google_vertex_ai_reasoning_engine`) are subject to the beta-provider's faster churn. The schema may evolve in 7.31+ in ways that surface as plan diffs. Standard `terraform plan` discipline catches this.

## How this was applied

```bash
# 1. Edited the 6 versions.tf files (and foundation/main.tf), 5.30 → 7.30
# 2. terraform init -upgrade  (downloaded google v7.30.0 + google-beta v7.30.0)
# 3. terraform plan           (1 to change: project deletion_policy)
# 4. User approved
# 5. terraform apply -auto-approve
# 6. terraform plan           (No changes)
# 7. pytest                    (155 passed)
```

## Related

- PR 4b will use `google_vertex_ai_reasoning_engine` (in `google-beta`) for the Triage Agent deployment.
- `scripts/model_armor_check.py` already enforces a `model_armor_config { ... }` block on any RE matching `asb-agent-triage*` — that gate will now have a real resource to bind to.
- Future agents (Knowledge Surfacer per PRD §4.4, Risk Watcher) will share this resource type.
