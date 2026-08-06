# ADR 0019 — Triage Pub/Sub bridge as Cloud Run Job + Cloud Scheduler

**Status:** Accepted
**Date:** 2026-04-29
**Workstream:** WS-G1 (Triage Agent), PR 4d

## Context

WS-G1 PR 4c shipped the Triage Reasoning Engine
(`projects/.../reasoningEngines/0000000000000000000`) and PR 3 shipped
the Pub/Sub subscription `asb-triage-input-sub`. They are not connected:
nothing pulls the subscription. Triage is code-complete but operationally
idle — Gmail / Airtable signals don't reach the classifier.

PR 4d builds the bridge. Three viable hosts:

1. **Cloud Run Job + Cloud Scheduler** — pull the subscription on a cron
   tick, batch-ack at end. Same shape as the existing airtable sync
   (ADR 0010).
2. **Cloud Function (Gen2) with Pub/Sub push trigger** — event-driven,
   per-message invocation. Fastest end-to-end but requires enabling
   `cloudfunctions.googleapis.com`.
3. **Eventarc → Cloud Run service** — most flexible, also requires a new
   API (`eventarc.googleapis.com`) and a long-running service rather than
   a job.

PRD §6.2 sets the SLA at "10 min from signal landed to classification
written" with retry headroom.

## Decision

Run the bridge as a **Cloud Run Job triggered by Cloud Scheduler every 5
minutes**. Resources land in `terraform/modules/agent_runtime/triage_bridge.tf`:

- Cloud Run Job `asb-triage-bridge`, runs as the existing
  `asb-agent-triage-sa` (already has `pubsub.subscriptions.consume`,
  `aiplatform.reasoningEngines.query`, BQ writer for triaged_items, BQ
  writer for audit log).
- Cloud Scheduler `asb-triage-bridge-5m` posts to the job's `:run` endpoint
  with OIDC auth as a new invoker SA `asb-triage-bridge-invoker-sa`
  (mirrors the airtable sync's two-SA pattern).
- New Artifact Registry repo `asb-agents` for agent worker images. Kept
  separate from `asb-sync` because the latter is logically scoped to data
  syncs; future agent workers (Risk Watcher, Morning Brief) share `asb-agents`.

Per-message processing is a `BaseAgent` subclass (`RemoteTriageAgent` in
`src/agency_brain/agents/triage/bridge.py`) whose `_run` calls
`vertexai.agent_engines.get(re_resource_name).query(**payload)` and writes
to `agent_outputs.triaged_items`. Reusing `BaseAgent` gives us HIPAA
pre-flight, audit emission on every code path, and confidence-based
human-review routing for free.

Acks: success ⇒ ack. RE returns `internal_error` ⇒ no-ack (Pub/Sub
redelivers up to 5× then dead-letters to `asb-triage-input-dlq`).
Malformed JSON / missing required fields / RE-side `invalid_input` /
`hipaa_guard_tripped` ⇒ ack (poison; the message can never succeed).

## Why not Cloud Function (Gen2)

- Requires enabling `cloudfunctions.googleapis.com`. Every additional API
  is one more surface area for IAM, audit, and cost — for a 2-person tool,
  the marginal value of "near-real-time" classification doesn't justify
  the new runtime pattern.
- The PRD §6.2 SLA is 10 min, and a 5-min scheduler cadence + ~30s job
  runtime sits comfortably inside that with retry headroom. Faster
  invocation provides no contractual benefit.
- Pub/Sub push triggers are idempotent-by-default but require the handler
  to ack via HTTP response code, which couples failure semantics to the
  HTTP layer. Pull semantics (this ADR) keep Pub/Sub's retry/DLQ behavior
  on the Pub/Sub side, where it's already configured per
  `triage_pubsub.tf`.

## Why not Eventarc + Cloud Run service

- Adds a third runtime pattern (long-running Cloud Run service) on top of
  the existing two (Cloud Run Job, Reasoning Engine). For a 2-person tool,
  consolidating on the Cloud Run Job pattern reduces operator burden.
- Same "new API" cost as Cloud Function plus a "long-running service
  always billing" footprint. The triage volume (Gmail signals at small
  agency cadence) doesn't justify continuous invocation.

## Why reuse `asb-agent-triage-sa` instead of a new bridge SA

- The triage SA already has the exact IAM the bridge needs (Pub/Sub
  consume, RE query, BQ writes to triaged_items + audit). A new bridge
  SA would duplicate every binding for no security gain — the bridge's
  privileges are a strict subset of what the deployed RE itself needs.
- Audit attribution stays consistent: every `agent_audit_log.events` row
  carries `sa_email = asb-agent-triage-sa`, regardless of whether the
  classification came from a direct RE query or from the bridge.
- The Cloud Scheduler invoker SA is separate (`asb-triage-bridge-invoker-sa`)
  so the OIDC-invocation surface is least-privilege (`run.invoker` only).

## Consequences

- The repo owns one new Dockerfile (`Dockerfile.triage-bridge`), one new
  cloudbuild step, and one new Artifact Registry repo. Standard surface.
- Bootstrap requires a placeholder image at the `bootstrap` tag before
  first apply — same operator step as airtable sync (`gcloud builds submit
  --tag=...:bootstrap`). The Cloud Run Job's `lifecycle.ignore_changes`
  on the image attribute lets `gcloud run jobs update` swap tags
  post-merge without Terraform drift.
- 5-min cadence means worst-case latency is ~5 min + job runtime. If
  signal volume grows or SLA tightens, the cadence is a single TF variable
  change (`triage_bridge_schedule`). Faster than 1 min would burn Cloud
  Run starts unnecessarily; slower than 5 min misses the SLA on the worst
  case.
- The `asb-agents` AR repo is a new resource. Empty repo cost is $0 today;
  the cleanup-policies pattern from ADR 0017 (cost guardrails, when it
  lands) should be applied here too.

## References

- PRD §6.2 (signal-to-classification SLA)
- PRD §6.3 (Triage Agent specification)
- ADR 0006 (audit log streaming + emit-on-every-path — `BaseAgent` reuse
  is a direct application of this contract)
- ADR 0010 (Cloud Run Job for the airtable sync — this ADR mirrors that
  shape one-for-one)
- ADR 0016 (Reasoning Engine deploy via SDK + import — the resource this
  bridge invokes)
- `terraform/modules/agent_runtime/triage_bridge.tf` (the resources)
- `src/agency_brain/agents/triage/bridge.py` (the entrypoint)
