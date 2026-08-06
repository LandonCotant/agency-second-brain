# ADR 0061 — Retire the Triage Reasoning Engine; run classification in the bridge job

**Status:** Accepted — 2026-06-03

> **Implementation status (2026-06-03):** Code landed on branch
> `feat/retire-triage-reasoning-engine` — classification runs in-process in the
> bridge (steps 1–6 below). **Pending cutover (operator):** rebuild/roll out the
> bridge image → prod smoke → delete the RE (`deploy_triage_re.py --delete`) →
> cleanup PR (remove `agent.py`, `triage_reasoning_engine.tf`, deploy scripts).
> The RE stays live but idle until the smoke passes, so rollback = redeploy the
> prior bridge image tag.
**Workstream:** WS-G1 (Triage) + cost guardrails
**Supersedes (in part):** PRD §6.3 (RE-as-deploy-target mandate)
**Related:** ADR 0016 (RE deploy via SDK + TF import), ADR 0019 (Cloud Run Job + Scheduler bridge), ADR 0024 (cost guardrails), ADR 0028 (RE creation alert)

## Context

The 2026-06-03 cost audit found the Vertex AI Reasoning Engine `asb-agent-triage`
(`reasoningEngines/0000000000000000000`) is the single largest line in the whole
GCP bill. The billing export shows:

- May 20–26: ~250–700 vCPU-seconds/day — absorbed by the 50 vCPU-hr/mo free tier → ~$0.
- **From ~May 29–30: ~260,000–280,000 vCPU-seconds/day = ~3.2 vCPU held 24/7**,
  exhausting the monthly free tier in under a day. Run-rate **~$60–150/mo**.

The engine `spec.updateTime` is 2026-05-01 — **no recent deploy caused this**.
The Agent Engine platform began holding warm replicas (~3 vCPU) for a workload
that does a few seconds of work every 5 minutes. We are paying for a persistent,
always-hosted runtime at ~0.1% utilization. On a $50/mo budget (ADR 0024) this
one resource exceeds the entire budget.

### Why the RE exists today
PRD §6.3 mandated Triage deploy as a Reasoning Engine so prompt versions ship as
`runtimeRevisions` for safe rollback. ADR 0016 implemented that. At build time
(2026-04-29) there was **no per-service cost visibility** (billing export was not
yet enabled — it was turned on during the ADR 0028 incident response on 05-02),
so the cost shape was invisible when the decision was made.

### Why the RE is the wrong tool for *this* workload
- Triage is a **single stateless `generate_content` call** with a JSON-schema
  output (`vertex_classifier.VertexClassifier`). No agent loop, no tool
  orchestration, no Sessions/Memory Bank (the audit confirmed **zero** Sessions/
  Memory/Code-Execution charges). The managed runtime's value-add is unused.
- Agent Engine bills a **management fee per vCPU-hr while hosted, idle or not**.
  A 5-min cron is the wrong billing shape for an always-hosted runtime.
- The RE already requires a Cloud Run job (`asb-triage-bridge`, ADR 0019) in front
  of it to pull Pub/Sub and call `query()`. We pay for **both** the job and the
  managed RE; the job can do the classification itself.
- The SDK-create-then-TF-import deploy pattern (ADR 0016) is orphan-prone — it
  caused the $40/day three-orphan incident (ADR 0028).

ADR 0028 already prescribed the direction ("the live triage RE is updated, not
recreated; use Vertex SDK direct"). This ADR completes that direction.

## Decision

**Retire the `asb-agent-triage` Reasoning Engine. Run triage classification
in-process inside the existing `asb-triage-bridge` Cloud Run job**, calling Gemini
via the Vertex SDK directly.

The bridge already runs as `asb-agent-triage-sa` and pulls `asb-triage-input-sub`.
The change swaps its `RemoteTriageAgent` (which network-calls the RE) for the
**local `TriageAgent`** (`agents/triage/triage_agent.py`) that already exists in
the codebase and is what the RE wraps.

### Rollback mechanism (replaces `runtimeRevisions`)
Prompt/version rollback moves to **container image tags** — the mechanism all 15
other agents already use (ADR 0019). Roll back a bad prompt by redeploying the
prior `asb-triage-bridge` image tag. We accept the loss of RE `runtimeRevisions`.

## Implementation plan

1. **Code** — in `agents/triage/bridge.py`, construct the local `TriageAgent`
   (build `VertexClassifier` + `goal_context`/`sender_context` loaders +
   `writers`) instead of `RemoteTriageAgent`. Keep the Pub/Sub pull, BQ writes,
   and audit emit unchanged. Delete the `engine.query()` path and the
   `TRIAGE_RE_RESOURCE_NAME` dependency.
2. **Dockerfile** — `Dockerfile.triage-bridge` already pins
   `google-cloud-aiplatform` (vertexai). Reconcile the **new dep boundary** per
   the CLAUDE.md gotcha: the local path pulls the Airtable write client + context
   loaders that the RE previously hosted. Grep the local `TriageAgent` import
   tree against the Dockerfile pin set; add any missing (likely the Airtable
   client + its transport). **This will fail at runtime, not build time, if
   missed** — smoke-fire before merge.
3. **Smoke test in prod** — fire the bridge once against a real Pub/Sub message;
   confirm an `agent_audit_log.events` row with `success=true`, matching
   classification vs. the RE baseline (ADR 0016 recorded confidence=0.95).
4. **Only after smoke passes** — delete the RE:
   `python scripts/deploy_triage_re.py --delete projects/.../reasoningEngines/0000000000000000000`
5. **Terraform** — remove `triage_reasoning_engine.tf`, the
   `TRIAGE_RE_RESOURCE_NAME` env on the bridge job, and (optionally) the
   `*-agent-artifacts` `triage/` staging objects. `terraform plan` must be clean.
6. **Docs** — amend PRD §6.3; update `docs/PRODUCTION_STATE.md`; keep the ADR 0028
   RE-creation alert (still useful as a tripwire against accidental re-creation).

## Consequences

- **Saves ~$60–150/mo** (the current RE run-rate) — the largest single lever in
  the audit, ≈ the entire monthly bill. Bridge job CPU rises slightly (it now
  runs the model call) but stays scale-to-zero between 5-min ticks.
- Loses managed `runtimeRevisions`; gains image-tag rollback consistent with
  every other agent. One fewer deploy pattern to maintain (no SDK-deploy +
  TF-import dance, no orphan-RE risk).
- The ADR 0028 creation alert remains as a guardrail.
- Model Armor: already disabled at the call site (ADR 0017) — unaffected.

## Alternatives considered

- **`adk deploy cloud_run`** — Google's documented path to run agents on Cloud
  Run (scale-to-zero) instead of Agent Engine. Not needed here: triage isn't an
  ADK agent and we already have the bridge Cloud Run job. Folding in is simpler.
- **Set RE min-replicas to 0** — not reliably exposed on the resource; even if it
  were, the bridge already gives true scale-to-zero for free.
- **Keep the RE, accept the cost** — rejected; ~$60–150/mo for an unused managed
  runtime exceeds the whole budget (ADR 0024).
