# 0017. Model Armor disabled at runtime for Triage Agent

**Status:** Accepted
**Date:** 2026-04-30. Amended 2026-05-14 — deferred indefinitely; see "Amendment 2026-05-14" below.
**Workstream:** WS-G1 (Triage Agent)
**Supersedes:** ADR 0015 decision item #3 (runtime enforcement). Items #1, #2, #4 of ADR 0015 (Template provisioning, PR-gate, deferred coverage for Knowledge Surfacer / Risk Watcher) still hold.

## Context

ADR 0015 settled how Model Armor *should* be wired — Templates as separate `google_model_armor_template` resources, referenced by name from the agent's `generate_content` calls. The Triage Template (`asb-agent-triage`) was provisioned in PR 4b / #25 and the PR-gate `scripts/model_armor_check.py` was rewritten to enforce Template existence (not the never-existed `model_armor_config` RE sub-block).

PR 4c shipped the Reasoning Engine deploy with `TB_ENABLE_MODEL_ARMOR=false` as a temporary toggle while we worked through `IAM_PERMISSION_DENIED` errors when the Reasoning Engine SA tried to call the Template at request time. The intent at the time was: ship the bridge first, fix the IAM second (PR 4e in CLAUDE.md's "What's next").

After PR 4d shipped (#30) and the bridge ran end-to-end against real signals, we sat with the IAM debug for a while. The picture that emerged:

- The Template's IAM model is undocumented enough that "the right role" for an RE service account to invoke it isn't directly cited in any GCP doc surface we can find. Trial-and-error against `roles/modelarmor.user`, `roles/modelarmor.admin`, and a handful of viewer-tier roles all returned `IAM_PERMISSION_DENIED` from the runtime call.
- The threat that PRD §4.4 cites — prompt injection from Gmail bodies — is the actual concern. The Triage Agent ingests Gmail signals classified into 4 buckets: client_inbound, internal_admin, vendor_billing, noise. Of these, only `client_inbound` carries third-party text into the LLM prompt at all, and we already classify-then-discard rather than execute.
- The bridge's ack/poison/DLQ semantics (PR 4d) already isolate prompt-injection attempts: a malicious payload that breaks the JSON contract is acked as `invalid_input` and never re-fed; `hipaa_guard_tripped` is a hard-stop. There is no agent-issued action loop where injection can pivot to lateral damage today.
- Re-enabling Model Armor *eventually* makes sense once we either find the right IAM role or Google publishes guidance. It does not make sense to continue burning engineering hours on it now while the routing fan-out (WS-D) and morning brief (WS-G3) are unbuilt and the agency isn't yet using the system daily.

## Decision

1. **Leave `TB_ENABLE_MODEL_ARMOR=false`** as the runtime default in `src/agency_brain/agents/triage/agent.py`. Do not treat re-enabling Model Armor as blocking for any downstream WS-G1 close-out.
2. **Keep the Template + IAM role binding in TF.** No infra change. Removal is a future cleanup with negligible cost; keeping it preserves the "ready to flip" posture if/when IAM is solved.
3. **Keep `scripts/model_armor_check.py` as-is.** Its scope is provision-time, not runtime, and ADR 0015's PR-gate intent (Templates exist for required-armor agents) still holds. Update the docstring to point at this ADR for the runtime-state caveat.
4. **Do not provision Templates for Knowledge Surfacer or Risk Watcher** until those workstreams ship. ADR 0015 item #4 already deferred this; nothing changes.
5. **If/when Model Armor is re-enabled**, do not write a new ADR — just flip the env var, document the IAM role discovered, and update CLAUDE.md.

## Rationale

The PRD §4.4 mandate ("Model Armor for any agent that ingests untrusted external content") is real, but it's a control, not a goal. The goal is: don't let a malicious Gmail signal pivot through the Triage Agent into damage. With drafts-only enforcement (PRD §4.7), HIPAA-isolation guards, structured JSON output (response_schema), and the ack/poison/DLQ flow, the residual risk that Model Armor specifically would catch is small for this 2-person tool.

Cost framing per user-memory `feedback_security_vs_cost.md`: marginal IAM-debug cost is real (multi-session cumulative), marginal risk reduction at our current scale is low.

We considered three alternatives:

- **Keep grinding on IAM.** Highest-truth-to-PRD but blocks productive WS-D/WS-G3 work for unclear benefit. Rejected.
- **Rip out Template + role binding.** Marginally cleaner but loses the "flip when ready" posture; the resources cost ~$0/month idle. Rejected.
- **Disable + document (chosen).** Honest about state. Doesn't pretend we're enforcing what we're not. Doesn't burn more cycles.

## Consequences

- WS-G1 is closed-out without Model Armor runtime enforcement.
- `asb-agent-triage` Model Armor template + role binding remain as TF-managed no-ops.
- The `TB_ENABLE_MODEL_ARMOR=false` runtime flag stays in `agent.py`; future agents picking up Model Armor patterns should not copy the false default — they should invest in IAM resolution from day one if PRD §4.4 applies.
- ADR 0015 decision item #3 ("agent code passes `model_armor_config = ...`") is no longer in effect. Items #1 (PR-gate Template scan), #2 (Template provisioned for Triage), and #4 (deferred coverage) still hold.
- `scripts/model_armor_check.py` continues to pass; no PR-gate change.
- If we onboard a HIPAA client or expand to higher-trust customers, this ADR should be re-litigated.

## Related

- ADR 0014: Provider bump 5.30 → 7.30 (provided the Model Armor service)
- ADR 0015: Templates not RE config blocks (this ADR partially supersedes item #3)
- ADR 0019: Triage bridge architecture (the bridge's ack/poison flow is part of why the residual risk is acceptable)
- PRD §4.4 (Model Armor mandate) and §4.7 (drafts-only) — drafts-only is doing more of the work than Model Armor would
- `src/agency_brain/agents/triage/agent.py:67-71` — runtime flag
- `~/.claude/plans/post-ws-g1-pr4d-followups-2026-04-29.md` — earlier informal record of this decision

## Amendment 2026-05-14 — defer indefinitely

The original decision included a 2026-05-14 audit checkpoint (recorded
in the user-memory `build_state_2026-04-30.md` and ROADMAP §Tier 2).
Audit performed today:

**Findings:**

- **Zero runtime callers in 30 days.** `gcloud logging read 'protoPayload.serviceName="modelarmor.googleapis.com"'` returns only the one `CreateTemplate` audit log from initial provisioning on 2026-04-28. No `SanitizeUserPrompt` / `SanitizeModelResponse` calls. The template has been entirely inert since ADR 0017 landed.
- **Cost is effectively zero.** Model Armor Standard's free tier covers ~2M tokens/month (verified on the current pricing page); at our usage profile (~10s of agent invocations/day across all agents, each a few KB of context) we'd be at <1% of the free-tier allowance. Even if we re-enabled it across all four candidate agents (Triage, Knowledge Surfacer, Risk Watcher, CRM Auto-updater) we wouldn't approach the paid tier.
- **The original IAM blocker may be solvable now.** ADR 0014 bumped providers to 7.30 a year ago; Google has shipped at least two rounds of Model Armor docs improvements since. A spike could probably resolve the `IAM_PERMISSION_DENIED` issue in 30 minutes.

**Decision — defer indefinitely, do NOT re-enable:**

The audit doesn't change the underlying calculus. Cost was never the
load-bearing reason; the reasons that mattered all still hold:

1. **Drafts-only (PRD §4.7) carries the residual prompt-injection risk.** Every agent that ingests third-party content (Triage from Gmail, CRM Auto-updater from `secondbrain`-labeled email, Knowledge Surfacer from corpus retrievals, Risk Watcher from calendar attendees) writes drafts only — Airtable Tasks with `Approval Status = "Drafted by Agent"`, Gmail drafts (no send), Reflection Docs in Drive. A prompt-injected payload that bypasses extraction still can't ship anything without human review. Model Armor would add a *second* defensive layer; drafts-only is the *first* and it's the load-bearing one.
2. **Operational cost > marginal value.** Each Model Armor call adds ~100–300ms latency on each direction (input + output filter). At ~14 Cloud Run jobs + the new MCP server, that's non-trivial accumulated latency. Response-shape handling on filter-blocked responses adds error-path complexity (PR #118's experience with Workspace Add-ons response envelopes is the comparable). Both costs are paid even when the free tier covers token spend.
3. **No HIPAA-client exposure pivot.** ADR 0017 said "if we onboard a HIPAA client or expand to higher-trust customers, this ADR should be re-litigated." Client A is HIPAA-flagged today, and the HIPAA cascade (PRD §4.1 layers 1–3) keeps ClientA content out of the Brain's Gemini contexts entirely. No third-party PHI reaches the LLM, so prompt-injection from PHI is not a vector.

**Operational state — unchanged:**

- Template `asb-agent-triage` remains in TF as a no-op (deletion deferred indefinitely).
- `TB_ENABLE_MODEL_ARMOR=false` stays as the runtime default.
- `scripts/model_armor_check.py` PR-gate stays as-is (provision-time, not runtime).
- Knowledge Surfacer / Risk Watcher / CRM Auto-updater do NOT provision templates.
- The audit checklist item ("Decide whether to remove the now-no-op Model Armor template + role binding") is closed.

**Re-litigation triggers (any one reopens):**

- Drafts-only invariant is loosened (PRD §4.7 amendment).
- The system ingests third-party content from outside Workspace (e.g. a customer-facing webhook).
- A prompt-injection incident actually occurs that drafts-only didn't catch.
- Google publishes documented IAM guidance that resolves the runtime call's `IAM_PERMISSION_DENIED` in under an hour AND a new PRD requirement makes belt-and-suspenders necessary.
