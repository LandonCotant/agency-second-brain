# ADR 0028 — Alert on Reasoning Engine creation events (cost guardrail)

**Status:** Accepted
**Date:** 2026-05-02
**Workstream:** WS-E (observability) + cost guardrails

## Context

On 2026-05-02 the Brain project's GCP spend was running ~$40/day, ~24×
the $50/mo budget alert threshold from ADR 0024. Investigation found
**three orphan Vertex AI Reasoning Engines** from deploy iterations on
2026-04-29 still hosted alongside the live one. Vertex AI Agent Engine
bills per vCPU-hour + memory-GB-hour while hosted, idle or not, so 4
engines was ~4× expected runtime cost.

Root cause: ADR 0016 mandates "deploy via Python SDK, then `terraform
import`" because the TF resource is unstable for create. Two iterations
on 2026-04-29 produced new engines without deleting the prior ones.
There was no monitoring signal that this had happened — the only visible
symptom was the daily billing total, and we had no per-service cost
visibility (billing export to BigQuery was not enabled).

## Decision

Add a **log-based metric + Cloud Monitoring alert** that fires on any
`google.cloud.aiplatform.v1.ReasoningEngineService.CreateReasoningEngine`
audit log event in the Brain project, routed to the existing
`Brain alerts` Chat space and the owner-email channel.

Semantics: *every* RE creation pings the operator, even legitimate ones. The
operator's job on receipt is to confirm the prior engine was deleted.
This catches the actual incident pattern (deploy without cleanup) at
the cost of one expected-and-acknowledged ping per real deploy. For a
2-person tool with infrequent RE deploys this is acceptable noise.

## Why not a dedicated audit Cloud Run Job

The existing audit framework (ADR 0012) has 4 Cloud Run Jobs each with
their own SA, custom IAM role, image, scheduler, and BQ audit emit
plumbing. Adding a 5th to poll `aiplatform.reasoningEngines.list` and
diff against an expected count is enterprise ceremony for a single
drift check that fires on a state transition Cloud Audit Logs already
captures for free.

The log-based-metric path is pure Terraform, reuses existing
notification channels, and has zero runtime cost.

## What this does NOT cover

- An RE that was created *before* the metric was deployed and lingers.
  (Not a concern as of 2026-05-02 — count is 1 and verified.)
- An RE created via a path that bypasses Cloud Audit Logs. There is no
  such path; all Vertex AI control-plane mutations are audited by
  default.
- Daily $-amount visibility. That's the **billing export to BigQuery**
  follow-up enabled in the same incident response — it lives in the
  billing-account scope and is configured via Cloud Console, not TF.

## Consequences

- One Chat ping + one email per `CreateReasoningEngine` event in the
  Brain project. Auto-close after 7 days (matches ADR 0008 alert
  strategy).
- If RE deploy frequency rises substantially (it should not — the live
  triage RE is updated via `terraform import`, not recreated), revisit
  the alert threshold.
- The vestigial Model Armor template and the open question of removing
  it (ADR 0017) is unaffected.
