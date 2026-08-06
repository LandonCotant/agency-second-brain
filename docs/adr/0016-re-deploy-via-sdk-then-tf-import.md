# 0016. Reasoning Engine deploy via Python SDK, then `terraform import`

**Status:** Accepted
**Date:** 2026-04-29
**Workstream:** WS-G1 (Triage Agent)
**Related:** ADR 0014 (provider bump), ADR 0015 (Model Armor via Templates)

## Context

PRD §6.3 mandates Triage be deployed as a Vertex AI Reasoning Engine (Agent Engine) so prompt versions can ship as `runtimeRevisions` for safe rollback. ADR 0014 unlocked the `google_vertex_ai_reasoning_engine` resource by bumping providers to 7.30.

When we drafted the deploy, we discovered the resource has **partial Terraform coverage** for the deploy lifecycle:

- The TF resource manages the resource record (display_name, region, labels, IAM bindings) but cannot itself **package and upload** the agent's Python source + dependencies.
- The deploy artifact is built by `vertexai.agent_engines.create()` (the Python SDK), which:
  1. Detects the agent's import dependencies via runtime introspection
  2. Serializes the agent object via `cloudpickle`
  3. Tarballs `extra_packages`
  4. Uploads all three to GCS staging
  5. Calls the RE create API
- Reproducing that in raw Terraform would require duplicating the SDK's packaging logic — brittle, and likely to drift as the SDK evolves.

Three deploy paths exist on the resource (`spec.container_spec`, `spec.source_code_spec.python_spec`, `spec.package_spec`); the SDK chooses + manages whichever it likes.

## Decision

**Split deploy management between the Python SDK and Terraform**, intentionally and explicitly:

- **First deploy + every prompt-revision rollout**: run `scripts/deploy_triage_re.py` (or analogue), which calls `vertexai.agent_engines.create()`. This is the canonical path for ADK-based agents and matches Vertex AI documentation.
- **Resource record in Terraform**: declare `google_vertex_ai_reasoning_engine.tb_agent_triage` with `lifecycle.ignore_changes = [spec, labels, description, project]`. After the first SDK deploy, run `terraform import` to bring the resource into state. The TF resource records:
  - `display_name`
  - `region`
  - The fact that this RE exists (so other TF resources can reference it, and so destroy/recreate can be expressed)
- **IAM, GCS bucket, Pub/Sub subscriptions, Model Armor templates**: all stay in Terraform (PR 3, PR 4a, PR 4b). Only the RE resource itself is split.

## Rationale

We considered three alternatives:

1. **Reproduce the SDK's packaging in Terraform** (use `inline_source.source_archive` with a base64 tarball + manifest). Tried briefly — the tarball would balloon TF state, and the SDK does smart things (auto-detecting requirements from imports, picking the runtime Python version) that are non-trivial to recreate in HCL.

2. **Skip Terraform for the RE resource entirely; manage everything via SDK + scripts**. Loses TF's drift detection on the resource's existence and would make the RE invisible to `terraform plan`. Future changes to neighboring resources (IAM, Pub/Sub) wouldn't see the RE in state.

3. **Hybrid (chosen)**: SDK builds the artifact + creates the resource; TF imports + manages the record with extensive `ignore_changes`. Gets us drift detection for the resource's existence + display_name + region, while letting the SDK own the packaging lifecycle. This split is explicit in the TF file's documentation (and this ADR).

Trade-offs:

- **Bootstrapping is two-step**: first `python scripts/deploy_triage_re.py`, then `terraform import ...`. Documented in `triage_reasoning_engine.tf`.
- **Future RE properties won't be TF-managed**: if Google adds a `min_replicas` field we want to set, we'd manage it via SDK env vars + ignore_changes, not by adding it to TF. Acceptable for now.
- **A destroy via TF works** (the `delete()` API call is identical) — the RE resource can be cleaned up the standard way.

## What this PR does

1. `terraform/modules/agent_runtime/triage_reasoning_engine.tf` declares the resource with `ignore_changes = [spec, labels, description, project]`.
2. `scripts/deploy_triage_re.py` is the deploy entrypoint. Includes `--invoke-only` for re-testing and `--delete` for cleanup.
3. The first deploy succeeded as `asb-agent-triage` at `projects/000000000000/locations/us-central1/reasoningEngines/0000000000000000000`; imported via `terraform import`. `terraform plan` returns "No changes" post-import.
4. End-to-end smoke test verified: `query()` invocation → real Gemini classification → JSON-schema-controlled response → BaseAgent audit row in `agent_audit_log.events` (`success=true, hipaa_guard_status=PASSED, confidence=0.95, latency_ms=5674`).

## v0 deferral

Model Armor enforcement (PRD §4.4 / ADR 0015) is **disabled at the call site** for the v0 deploy. The Template (`asb-agent-triage`, PR 4b) is provisioned and the SA has `roles/modelarmor.user` (project-level), but the runtime call returned `IAM_PERMISSION_DENIED` we couldn't immediately resolve — likely a resource-level IAM the TF provider doesn't expose. Re-enabling is gated on:

- Setting `TB_ENABLE_MODEL_ARMOR=true` env var on the deployed RE
- Re-deploying via `scripts/deploy_triage_re.py`

This must land before PR 5's DoD #6 milestone ("Model Armor blocks ≥1 synthetic prompt-injection test").

## Consequences

- The RE is now in TF state; `terraform plan` is clean.
- Prompt iteration: edit `prompts/triage/v1.md` (or add `v2.md`), re-run `scripts/deploy_triage_re.py`, no TF changes.
- Schema iteration on `TriageOutput`: edit `models.py` + `vertex_classifier.py`'s `TRIAGE_RESPONSE_SCHEMA`, re-deploy.
- Model Armor (when re-enabled): just flip the env var and re-deploy. No code change needed.
- Future agents (Risk Watcher, Knowledge Surfacer, briefers) will follow the same SDK-deploy + TF-import pattern.

## Related files

- `scripts/deploy_triage_re.py`
- `scripts/deploy_re_spike.py` (Phase 1 spike that proved the SDK path works)
- `terraform/modules/agent_runtime/triage_reasoning_engine.tf`
- `terraform/modules/agent_runtime/agent_artifacts_bucket.tf` (the GCS staging bucket)
- `src/agency_brain/agents/triage/agent.py` (the Queryable entrypoint)
- `src/agency_brain/agents/triage/vertex_classifier.py` (real Gemini impl with Model Armor wiring)
