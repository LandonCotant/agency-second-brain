# WS-C: Agent Runtime — Acceptance Criteria

Sign-off gate for merging the `ws-c-agent-runtime` branch. Per [PRD.md](../../PRD.md) §5.4, §6.1, §7.4.

**Depends on:** WS-A (merged).
**Wall-clock target:** 1.5 weeks.

## Functional checks

- [ ] **Base agent class.** `src/agency_brain/agents/base.py` provides:
  - HIPAA pre-flight check (aborts with `HIPAA_GUARD_TRIPPED` on `hipaa_excluded` aspect)
  - Audit log emission (every invocation, success or failure, with Agent Identity UUID)
  - Prompt loading from `prompts/` (versioned, never inline)
  - Confidence threshold enforcement (`< 0.7` → human review queue)
  - Memory Bank namespace helpers (read/write standard methods)
- [ ] **Memory Bank conventions.** Namespace pattern documented (e.g. `risk-watcher/{client_id}/baseline`); namespace list in `docs/memory_bank_namespaces.md`.
- [ ] **Audit log schema.** `agent_audit_log.events` BigQuery schema defined and merged to main (other workstreams depend on it). Columns per PRD §4.6: `event_id, timestamp, agent_id, agent_identity_uuid, sa_email, input_summary, output, confidence, latency_ms, cost_usd, hipaa_guard_status, model_armor_findings`.
- [ ] **Reasoning Engine deployment template.** Terraform module pattern for deploying an agent as a Reasoning Engine, with `runtimeRevisions` for prompt versioning. Smoke-deploy a hello-world agent end to end.
- [ ] **Model Armor config template.** Reusable HCL fragment for `ModelArmorConfig` so per-agent modules can apply it consistently.
- [ ] **Cached Contents pattern.** Helper for agents to cache stable per-invocation context (active goals for Triage, client baselines for Risk Watcher).
- [x] **agent_outputs.\* tables.** Schemas for `triaged_items`, `risk_flags`, `goals`, `goal_scores` defined and migrated (PR #3). Mixed canonicality (BQ-canonical for triage/risk; Airtable-canonical for goals) documented in [ADR 0009](../adr/0009-agent-outputs-schema.md).

## Security checks

- [ ] No SAs created in this module beyond the runtime infrastructure SA.
- [ ] All agent runtime resources tagged with `workstream = "agent_runtime"`.
- [ ] `scripts/model_armor_check.py` real logic merged: parses Reasoning Engine TF and verifies `ModelArmorConfig` on Triage, Knowledge Surfacer, Risk Watcher.

## Documentation checks

- [ ] `src/agency_brain/agents/README.md` and `src/agency_brain/common/README.md` explain the base class + shared utilities.
- [ ] `docs/memory_bank_namespaces.md` lists every namespace and its owner workstream.
- [ ] At least one ADR for any non-obvious decision (e.g. cost-optimization patterns, retry/circuit-breaker stance).

## Sign-off

- [x] the implementer — the implementer — date: 2026-04-25
