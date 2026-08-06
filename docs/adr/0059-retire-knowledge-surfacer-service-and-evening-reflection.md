# ADR 0059 — Claude app is the canonical interface; retire the GCP-side interactive surfaces

**Status:** Accepted — 2026-05-31
**Supersedes (in part):**
- ADR 0046 — Knowledge Surfacer model + retrieval + surface (the Cloud Run *service* + Chat `/ask` surface; the retrieval *library* survives)
- ADR 0050 — Brain API surface (`POST /api/ask`) — retired in full
- ADR 0040 §2 REFLECT-mode scheduler (Evening Reflection daily run paused; complements ADR 0056 which paused PROMPT mode)

**Extends:** ADR 0051 (Brain is a signal substrate; conversation lives behind an MCP seam) — this ADR finishes the cleanup 0051 §3 started but deferred.

## Context

ADR 0051 (2026-05-13/14) flipped the architecture: the Brain is a signal-generation + writeback substrate, and the conversation/composition layer lives in an off-the-shelf MCP client (the Claude app). The MCP server shipped 2026-05-14 with `brain_ask`, which runs the *same* retrieval pipeline as the Knowledge Surfacer (`knowledge_surfacer.retriever.Retriever`) in-process on the operator's Mac and lets Claude synthesize.

The original §3 of ADR 0051 flagged the Knowledge Surfacer synthesis step + Chat surface for deprecation, then the 2026-05-14 amendment narrowed the cut list and left the service deployed. It has sat as zero-traffic cruft since. The 2026-05-28 production-readiness audit confirmed it: **`/ask` received zero requests in 7 days**, and the dependency sweep found **no internal caller** of the service URL or `/api/ask`.

Two operator decisions on 2026-05-31 closed the question:
1. **The dashboard is permanently deferred.** The Claude app provides every interface the operator wants. This orphans `/api/ask` (ADR 0050), which existed only to feed the dashboard's Gemini Live tool calls.
2. **Evening Reflection is now run via a scheduled Claude workflow**, not the Cloud Run Job — the same move ADR 0056 made for Morning Brief / Evening Prompt.

## Decision

### §1 — Retire the Knowledge Surfacer Cloud Run service (ADR 0046 §3, ADR 0050)

Delete the `asb-knowledge-surfacer` Cloud Run service and all its dedicated infra:
- `google_cloud_run_v2_service.tb_knowledge_surfacer` + the gsuiteaddons invoker binding
- `asb-knowledge-surfacer-sa` + custom role `tbKnowledgeSurfacer` + its 3 dataset IAM grants
- The `brain_api_enabled`-gated `asb-brain-api-caller-sa` + invoker + token-creator (count=0 in prod — never created)
- The module variables, env wiring (`terraform/envs/prod/{main,variables}.tf`, `terraform.tfvars`), `Dockerfile.knowledge-surfacer`, `cloudbuild.knowledge-surfacer.yaml`
- The service-only Python: `agents/knowledge_surfacer/{main,chat_app,synthesizer,agent,guardrail,prompts}.py` + their tests

**Kept as a library:** `agents/knowledge_surfacer/{retriever,models}.py`. `brain_ask` imports `Retriever` directly (`mcp_server/tools/read.py`); deleting it would break the one knowledge surface the operator actually uses. The package docstring is rewritten to reflect its library-only role.

Rejected: **leave it deployed but dormant.** Cost is ~$0 at zero traffic (`min_instances=0`), so cost isn't the driver — surface-area is. The service carried a Cloud Run service, an SA, a custom role, a dual-OIDC verifier, and a manual Chat-App registration, all of which showed up as audit/maintenance weight (a recurring YELLOW) for zero capability. Per the security-vs-cost user preference, the marginal maintenance cost is real and the marginal value is zero.

### §2 — Retire the `/api/ask` Brain API surface (ADR 0050) in full

No library survives — `/api/ask` was a Flask route on the deleted service. The dashboard it served is permanently deferred. If a programmatic (non-MCP) caller is ever needed again, re-derive from the `Retriever` library rather than reviving the service.

### §3 — Pause the Evening Reflection REFLECT-mode scheduler (ADR 0040 §2)

Remove `ignore_changes = [paused]` on `asb-evening-reflection-daily` and let `paused = true` take effect, so Terraform actively enforces the pause — the identical mechanism ADR 0056 applied to `asb-evening-prompt-daily`. **The Cloud Run Job stays deployed** (one-line revert: re-enable the scheduler) because the replacement Claude workflow is new and unproven; revisit full deletion once it has a few weeks of reliable runs. The F4 migration of `evening_reflection/main.py` to `google.genai` (merged in #176) stays — it keeps the Job un-pausable.

### §4 — The dashboard is permanently deferred, not parked

ROADMAP's "Dashboard for at-a-glance state" v2 question is closed, not deferred. The Claude app (MCP read/write tools + scheduled Claude workflows) is the canonical operator interface. Reopening would need a new ADR.

## Consequences

- **One fewer Cloud Run service, SA, and custom role**; the only Cloud Run *service* in the codebase is gone (everything is Jobs again). Simpler audit surface; the production-readiness YELLOW on `/ask` is resolved by removal.
- **`brain_ask` is unaffected** — same retriever, same corpus, Claude-side synthesis.
- **The manual Chat-App registration** in the Chat API Console is now orphaned and should be de-registered by hand (it points at a deleted service URL). Documented in the retired runbook.
- **Apply-before-merge ordering is load-bearing here.** `scripts/sa_allowlist_check.py` scans *live* project SAs; the `asb-knowledge-surfacer-sa` allowlist entry can only be removed once the targeted `terraform destroy` has actually deleted the SA, else the gate flags it as unallowlisted. The targeted destroy is applied first (operator-approved), then the allowlist entry comes out, then CI goes green, then merge.

## Cross-reference

- ADR 0046 — Knowledge Surfacer (service + Chat surface superseded; retriever library survives)
- ADR 0050 — Brain API surface (`/api/ask`) — retired in full
- ADR 0051 — Brain as MCP substrate (the decision this ADR completes)
- ADR 0056 — Morning Brief / Evening Prompt scheduler retirements (the pattern §3 mirrors)
- ADR 0028 — no new Reasoning Engines (unaffected; the triage RE stays)
