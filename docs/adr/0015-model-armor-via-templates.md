# 0015. Model Armor enforcement via Template resources, not RE config blocks

**Status:** Accepted (decision item #3 superseded by ADR 0017 on 2026-04-30 — runtime enforcement abandoned; Template + PR-gate still hold)
**Date:** 2026-04-28
**Workstream:** WS-G1 (Triage Agent)
**Supersedes:** none. Corrects an aspirational pattern in `scripts/model_armor_check.py` and the WS-G1 PR 4 plan.
**Superseded by:** ADR 0017 (decision item #3 only — agent runtime no longer passes `model_armor_config`)

## Context

PRD §4.4 mandates Model Armor for any agent that ingests untrusted external content: **Triage Agent**, **Knowledge Surfacer**, and **Risk Watcher** (all profiles).

The WS-A foundation work shipped a PR-time gate at `scripts/model_armor_check.py` that scans Terraform for `google_vertex_ai_reasoning_engine` resources, identifies required-armor agents by their `name`/`display_name`, and asserts a `model_armor_config { ... }` sub-block exists with `mode != "DISABLED"`. Test fixtures at `tests/fixtures/terraform/triage_*.tf` modeled the same shape.

PR 4 of WS-G1 was scheduled to deploy the actual Reasoning Engine. While drafting it we discovered that **`model_armor_config` has never been a field on `google_vertex_ai_reasoning_engine`** in any version of the Google or Google-beta provider, including `google-beta v7.30` (the latest, applied via PR 4a / ADR 0014). The PR-gate was enforcing an aspirational pattern that no real TF resource could satisfy.

The actual Model Armor API in 2026 has its own dedicated TF resources:

- `google_model_armor_template` — regional resource that defines a set of filters (prompt injection / jailbreak, malicious URI, Responsible AI category filters, sensitive-data-protection filters). Each filter has its own enforcement and confidence settings.
- `google_model_armor_floorsetting` — global, project-level safety floor.

Model Armor is then applied at the **API call level**: when an agent invokes `generate_content`, it passes the Template resource name in a `model_armor_config` parameter. The Vertex AI runtime applies the Template's filters to the prompt + response.

## Decision

1. **Replace the PR-gate's enforcement model.** Instead of looking for `model_armor_config` blocks inside RE resources, scan for `google_model_armor_template` resources. For each required-armor agent name pattern (`asb-agent-triage*`, `asb-agent-knowledge-surfacer*`, `asb-agent-risk-watcher*`), require at least one Template whose `template_id` matches the pattern AND whose `filter_config { ... }` contains at least one filter sub-block (`pi_and_jailbreak_filter_settings`, `malicious_uri_filter_settings`, `rai_settings`, or `sdp_settings`).

2. **Provision the Triage Template now**, in PR 4b:
   - Resource `google_model_armor_template.tb_agent_triage` in `terraform/modules/agent_runtime/triage_model_armor_template.tf`.
   - `template_id = "asb-agent-triage"`, regional (us-central1).
   - `pi_and_jailbreak_filter_settings { filter_enforcement = "ENABLED", confidence_level = "MEDIUM_AND_ABOVE" }` — Triage's primary threat is prompt injection through Gmail bodies.
   - `malicious_uri_filter_settings { filter_enforcement = "ENABLED" }` — Triage surfaces URLs from email signals.

3. **Update the Triage agent code (PR 4c)** so that the real Vertex classifier impl passes `model_armor_config = "projects/.../templates/asb-agent-triage"` on every `generate_content` call. This is the runtime enforcement; the Template alone doesn't apply filters until referenced.

4. **Defer Knowledge Surfacer + Risk Watcher Templates** until those workstreams ship. The PR-gate flags those patterns as uncovered today (after this PR merges), which forces those workstreams to provision their own Templates before they can pass CI. Acceptable: it's the same gate intent, just enforced at PR time.

## Rationale

The original PR-gate was set up before the Model Armor API stabilized; the team encoded "Model Armor must be enforced" as a pattern guess. The guess was wrong but the intent was right.

We considered three alternatives:

- **Wait for Google to add `model_armor_config` to the RE resource.** Two years of provider releases haven't done this; there's no signal it's coming. Templates are the documented API.
- **Per-call Python AST analysis at PR time.** Walk every agent's source for `generate_content` calls and verify they pass `model_armor_config`. More precise but harder to maintain — every agent's call site shape would need a regex.
- **Templates + provision-time enforcement (chosen).** Simpler PR-gate (HCL block scan) and aligns with how the GCP API actually works.

Trade-offs of the chosen approach:

- The PR-gate is provision-only — it doesn't catch an agent that has a Template but forgets to reference it in `generate_content`. **Mitigation**: PR 4c adds a unit test in the agent that verifies the classifier is constructed with a Model Armor template name; it'd fail CI if it isn't. Future workstreams will adopt the same pattern.
- Knowledge Surfacer + Risk Watcher will fail the gate after this PR until their own Templates land. **This is desirable** — it forces those workstreams to do the right thing rather than ship without armor.

## Consequences

- `scripts/model_armor_check.py` rewritten. 10 tests pass.
- 8 fixtures at `tests/fixtures/terraform/*.tf` rewritten to use `google_model_armor_template` resources.
- New TF resource `module.agent_runtime.google_model_armor_template.tb_agent_triage` deployed via PR 4b.
- PR 4c (Reasoning Engine deploy) drops the never-existed `model_armor_config` block from the RE TF, references the Template name from agent code instead.
- Future agents (Risk Watcher, Knowledge Surfacer) will need their own Templates before CI passes.

## How this was applied

```bash
# 1. Rewrote scripts/model_armor_check.py to scan for Templates.
# 2. Rewrote 8 fixtures under tests/fixtures/terraform/.
# 3. Rewrote tests/security/test_model_armor_check.py — 10 tests green.
# 4. Wrote terraform/modules/agent_runtime/triage_model_armor_template.tf.
# 5. terraform plan       (1 to add: tb_agent_triage Template)
# 6. User approved → terraform apply -target=...tb_agent_triage
# 7. terraform plan       (No changes)
# 8. pytest                (155 passed, +0 -0)
```

## Related

- ADR 0014: Provider bump 5.30 → 7.30 (unblocks the Model Armor service in google-beta).
- PR 4c will reference the Template from `TriageAgent`'s real Vertex classifier impl.
- PRD §4.4 (Model Armor mandate) and §4.8 (PR security gates) still hold — only the mechanism changed.
