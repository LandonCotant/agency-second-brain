# ADR 0023 — WS-D Chat fan-out via incoming webhook

**Status:** Accepted
**Date:** 2026-04-30
**Workstream:** WS-D (Routing & Delivery), first channel lane

## Context

WS-D's routing matrix MVP shipped in PR #40 — `route_item()` produces
`RoutingDecision` objects with `Channel.GOOGLE_CHAT_DM` intents for
`critical` and `high` severities — but nothing acts on them. Triaged
items land in `agent_outputs.triaged_items` with `routed_to = []` and
stay there.

PRD §6.5 specifies a Channel adapter per surface
(`channels/{gmail,chat,airtable,gemini_inbox}.py`). We pick **Chat** as
the first lane because Gmail is blocked on Workspace Domain-Wide
Delegation provisioning (no SA has `gmail.compose` today;
`audit/drafts_boundary_check.py:22` confirms DWD is "not yet
provisioned"). Chat is unblocked: the `Brain alerts` space already
exists (created during ADR 0008's notification-channel work) and the
single-recipient audience (the operator) makes a one-way send appropriate.

## Decision

Run Chat fan-out as a **Cloud Run Job triggered by Cloud Scheduler every
5 minutes**, mirroring the triage-bridge architecture (ADR 0019).
Resources land in `terraform/modules/agent_runtime/routing_fanout.tf`:

- Cloud Run Job `asb-routing-fanout`, runs as a new
  `asb-routing-sa` with BQ read on `agent_outputs.triaged_items`,
  BQ update on the same table (for `routed_to` writeback), audit-log
  writer on `agent_audit_log.events`, and Secret Manager accessor on
  `second-brain-gchat-webhook`.
- Cloud Scheduler `asb-routing-fanout-5m` posts to the job's `:run`
  endpoint with OIDC auth as a new
  `asb-routing-fanout-invoker-sa` (mirrors the triage-bridge two-SA
  pattern).
- Reuses the `asb-agents` Artifact Registry repo from ADR 0019. New
  image: `asb-agents/routing-fanout:bootstrap`.

Each tick:

1. Polls `agent_outputs.triaged_items` for rows where
   `'google_chat_dm' NOT IN UNNEST(routed_to)` AND
   `severity IN ('critical', 'high')` AND
   `triaged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL N MINUTE)`.
2. For each row, applies `route_item()` and inspects the leadership
   view's intents. `critical` always dispatches; `high` dispatches only
   if local time is in [09:00, 16:00) America/Los_Angeles (matches the
   matrix `same_day_if_before_4pm` cadence).
3. Posts the formatted message to the Brain alerts Chat space via
   incoming webhook.
4. On success, runs a BQ `UPDATE` to append `'google_chat_dm'` to
   `routed_to`. On failure, leaves `routed_to` unchanged so the next
   tick retries.

Per-tick processing is a `BaseAgent` subclass (`RoutingFanoutAgent` in
`src/agency_brain/routing/fanout.py`) whose `_run` does the
poll/dispatch loop. Reusing `BaseAgent` gives HIPAA pre-flight, audit
emission on every dispatch, and consistent error semantics with the
Triage Agent.

## Delivery mechanism: incoming webhook (not Chat API)

The Brain alerts space gets one Google Chat **incoming webhook**. URL
stored in Secret Manager (`second-brain-gchat-webhook`), accessed only
by `asb-routing-sa`. The fanout job POSTs JSON to that URL.

### Why not the Chat API with app credentials

- The Chat REST API requires either DWD-impersonation or a Chat-app
  registration in the Workspace. DWD is the same blocker that kept
  Gmail off the table for v1; Chat-app registration is one more
  Workspace-admin task with no operational benefit for a one-recipient
  setup.
- Webhooks were superseded by `google_chat` notification channels for
  **alert-policy alerting** (ADR 0008). That decision was driven by
  webhook-vs-Workspace-managed-channel reliability for monitoring
  alerts. It does not bind WS-D's routing-driven sends, where the
  payload is constructed by our code (not by Cloud Monitoring) and the
  retry semantics are handled by the Cloud Run Job + scheduler.

### Risk envelope

- The webhook URL is bearer-token-equivalent: anyone with the URL can
  post to the space. Mitigations: stored in Secret Manager with
  `roles/secretmanager.secretAccessor` granted only to
  `asb-routing-sa`; not committed to the repo; rotated via Workspace
  admin if leaked.
- Spoofing risk (an attacker with the URL impersonates the Brain) is
  bounded by the same one-recipient audience. the operator would notice an
  unexpected message in the Brain alerts space.

## Drafts-only boundary clarification (PRD §4.7)

PRD §4.7 says "no agent has any send/publish/modify capability against
external systems." Chat-to-Brain-alerts is a routing-driven *send*, not
a draft. We accept this for v1 because:

1. **Audience is the operator, not external clients.** PRD §4.7's load-bearing
   concern is preventing auto-sent emails/messages to clients. The
   Brain alerts space has exactly one human reader.
2. **No DWD scope is granted.** No agent SA acquires
   `chat.spaces.write` or any Workspace OAuth scope. The webhook path
   is IAM-invisible to GCP; from `drafts_boundary_check.py`'s
   perspective, no scope drift has occurred.
3. **Dispatch is severity-gated and idempotent.** The matrix limits
   sends to `critical`/`high` (not all triaged items), and `routed_to`
   prevents duplicates. The blast-radius of a misfire is one extra
   Chat line in the operator's own space.

`drafts_boundary_check.py` gets an explicit allow-note documenting that
Chat fan-out is a webhook-only path and does not require any new IAM
scope. The script's existing assertions (no `gmail.send` /
`gmail.modify` / `chat.spaces.write` scope on any SA) continue to hold.

This decision **does not extend** to Gmail or to multi-recipient Chat.
Gmail-drafts fan-out (the next lane) stays draft-only via
`gmail.compose`. Multi-recipient Chat (e.g. client-facing spaces) would
need a fresh ADR.

## Severity scope for v1

Dispatch on:

- `critical` — always, regardless of time
- `high` — only if local time in [09:00, 16:00) America/Los_Angeles

`medium`, `low`, `info` are deferred to the Morning Brief lane (WS-G3).
The orchestrator computes the decision in UTC throughout and converts
to local only at the dispatch gate, so DST handling stays in Python's
`zoneinfo` module rather than scattered through SQL.

## Idempotency

The `routed_to` REPEATED STRING column on `triaged_items` is the
authoritative dispatch ledger. The poll filter excludes rows where
`'google_chat_dm' IN UNNEST(routed_to)`; the post-send `UPDATE`
appends the channel marker.

Race window: a successful Chat post followed by a failed BQ `UPDATE`
results in one duplicate Chat message on the next tick. We accept this
because:

- The duplicate is visible (the operator sees two messages).
- A separate routing-attempts table would solve the race but adds a
  schema, a backfill, and a dependency for v1.
- Cloud Scheduler's max-1-instance default plus the 600s job timeout
  prevents overlapping runs from racing against each other.

If smoke testing surfaces measurable duplication, v2 can swap the
`UPDATE` for a `MERGE` with a `WHEN MATCHED AND ... THEN ...` clause
that re-checks the channel-not-already-in-array condition.

## Why one fan-out worker per channel (not a single multiplexer)

PRD §6.5 lists separate `channels/*.py` adapters but does not mandate
separate Cloud Run Jobs. We picked one job per channel because:

- IAM scope per worker is narrower (the future Gmail worker will hold
  `gmail.compose`-via-DWD; nothing else should).
- Channel-specific retry/backoff semantics stay isolated (Chat's
  webhook is rate-limited differently than Gmail's compose API).
- Operator burden is low: each channel is one TF file + one Cloud Run
  Job + one scheduler. The triage-bridge pattern is already proven.

## Consequences

- One new `Dockerfile.routing-fanout`, one new cloudbuild step, one
  new Cloud Run Job, one new Cloud Scheduler, two new SAs.
- Reuses the `asb-agents` AR repo (ADR 0019). No new repos.
- The webhook secret is created by TF as an empty secret; the version
  is added manually after webhook creation in the Workspace UI.
  Documented in `docs/runbooks/routing_fanout_setup.md`.
- The `routing/channels/` directory establishes the pattern for the
  Gmail / Airtable / Gemini Inbox adapters that follow.
- Future fan-out work (Gmail drafts, Airtable Tasks routing) lands as
  parallel `routing_fanout_*.tf` + `channels/*.py` files. The
  orchestrator (`fanout.py`) is parameterizable per channel.

## References

- PRD §6.5 (Routing layer), §4.7 (drafts-only boundary)
- ADR 0007 (Chat alerting via space webhook — superseded by ADR 0008
  for alert-policy alerting; not binding here)
- ADR 0008 (Chat alerting via google_chat channel — different concern)
- ADR 0019 (Triage bridge architecture — this ADR mirrors that shape)
- ADR 0006 (audit log streaming — `BaseAgent` reuse)
- `src/agency_brain/routing/matrix.py` (the decision function this
  ADR's worker dispatches on)
- `terraform/modules/agent_runtime/routing_fanout.tf` (resources, when
  TF lands in PR 2)
- `docs/runbooks/routing_fanout_setup.md` (manual webhook setup)
