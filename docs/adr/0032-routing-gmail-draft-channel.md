# ADR 0032 — Routing Gmail draft channel + multi-channel fan-out

**Status:** Accepted
**Date:** 2026-05-03
**Workstream:** WS-D (Routing & Delivery), second channel lane

## Context

WS-D Chat fan-out is live (ADR 0023, ADR 0025): `asb-routing-fanout`
polls `agent_outputs.triaged_items` every 5 minutes for unrouted
critical/high rows, posts a Chat card via webhook, and INSERTs a
`routed_events` row. WS-G3 Morning Brief shipped (ADR 0029) using
DWD with `gmail.compose` to draft a daily brief into the operator's
mailbox.

Per `CLAUDE.md`'s "What's next" Tier 1, the next routing channel is
a **Gmail draft fan-out**: pre-populate a Gmail reply for critical
and high client signals so the operator's response is one click instead of
starting from scratch. Drafts are non-notifying (sit in Drafts,
no push), so they're a low-cost addition with high ergonomic upside.

The building blocks already exist:
- `agents/morning_brief/gmail_drafts_client.py:38-79` — single-method
  `GmailDraftsClient.draft(...)` that POSTs `users.drafts.create`.
- `agents/morning_brief/main.py:271-310` (now lifted to
  `common/dwd.py`) — `DWDServiceFactory` that mints
  impersonated credentials with `gmail.compose` scope.
- ADR 0027 + 0029 DWD allowlist on `asb-agent-triage-sa`:
  `{gmail.compose, calendar.readonly}` already authorized.

## Decision

### 1. One multi-channel Job, not one Job per channel

Refactor `ChatFanoutAgent` → `RoutingFanoutAgent`. The agent now
takes a `channel_clients: dict[Channel, ChannelClient]` map at
construction; `_run` walks the matrix's leadership intents for the
row and dispatches to every channel that has a registered adapter
and is not already in `routed_events`. The Cloud Run Job
(`asb-routing-fanout`), scheduler (`asb-routing-fanout-5m`), image,
and SA (`asb-routing-sa`) are unchanged.

Trade-off accepted: a Gmail outage shares the Job execution with
Chat. Mitigation: per-channel try/except inside the dispatch loop —
a `GmailDraftError` records a transient error in
`output.error_channels[gmail_draft]` and continues to Chat;
`run_fanout_tick` aggregates per-channel errors for the exit code.

Rejected: parallel Cloud Run Job (`asb-routing-gmail-fanout`) — the
ceremony cost (second image, second scheduler, second SA token chain,
two LEFT JOINs against `routed_events`) is real and grows linearly
with channel count. The matrix already declares 5 channels; one Job
per channel does not scale.

### 2. Critical + high, always (no severity window)

Matrix:
- `_MATRIX["critical"]` adds `RouteIntent(GMAIL_DRAFT, "immediate", ...)`
  alongside the existing `GEMINI_INBOX` and `GOOGLE_CHAT_DM`.
- `_MATRIX["high"]` adds `RouteIntent(GMAIL_DRAFT, "same_day_if_actionable", ...)`
  alongside `MORNING_BRIEF` and `GOOGLE_CHAT_DM`.

Drafts are not gated by the 09:00–16:00 PT window that applies to
Chat `high`. Drafts don't notify — off-hours pile-up is acceptable
in exchange for the consistency of "every actionable signal has a
draft waiting." If volume turns out to be noisy in practice, future
ADR can re-add the gate.

### 3. Threading: design hook in v1, lights up later

Threading happens when the row carries a `gmail_thread_id`:
`_GmailAdapter.dispatch` passes it through to
`drafts.create({"message": {"raw": ..., "threadId": ...}})`. Today
no upstream stamps `thread_id` into the Pub/Sub envelope — `TriageInput`
(`agents/triage/models.py`) and the bridge
(`agents/triage/bridge.py:306-321`) only carry
`{source, source_url, source_event_ref, sender, subject, body,
ingested_at, aspects}`. The Gmail-to-Pub/Sub publisher is
"PR-4 deferred" per `docs/acceptance/ws-b-data-pipeline.md:16`.

Threading lights up automatically when WS-B PR-4 ships and stamps
`thread_id` into the payload (the bridge picks up new fields by
extending the row → context mapping in `fanout_main.row_to_input`).
**No DWD scope expansion** — `gmail.compose` already permits
threading via `threadId` in the create payload; no `messages.get`
read call is needed.

For non-Gmail sources (Drive, Calendar, Chat, Vantage), the adapter
defensively nullifies any `gmail_thread_id` it receives — a non-
Gmail thread id has no meaning to the Gmail API and would 4xx.

### 4. IAM: serviceAccountTokenCreator on asb-agent-triage-sa

`asb-routing-sa` (the routing fan-out runtime SA) gets
`roles/iam.serviceAccountTokenCreator` on the
`asb-agent-triage-sa` resource. SA-resource-scoped, not project-wide.
This lets the routing Job mint a `gmail.compose`-scoped credential
via the impersonated_credentials flow already in
`common/dwd.py:52-71`.

ADR 0027 §2 invariant preserved: `asb-agent-triage-sa` remains the
**only DWD-grantable SA**. The new binding lets `asb-routing-sa`
*impersonate* it at runtime — that's about who acts as the SA, not
who is DWD-grantable.

### 5. Recipient: always owner@example.com (v1)

Even when `triaged_items.owner_email` is non-null and not the operator,
v1 drafts always land in the operator's mailbox. Matches Morning Brief
precedent (ADR 0029 §6 deferred multi-recipient parity). Owner-
scoped drafts could be added later by minting a
per-recipient impersonated credential, but the dispatch volume
today does not justify it.

### 6. agent_id rename: routing-fanout-chat → routing-fanout

Now that the fan-out dispatches multiple channels, the
`-chat` suffix is misleading. Rename to `routing-fanout`. Old audit
rows remain searchable via `agent_id LIKE 'routing-fanout%'`. No
known external consumers grep for the exact string.

## Alternatives considered

- **Parallel Gmail-only Cloud Run Job** — see §1 rejection.
- **Threading via `messages.get`** — would need `gmail.metadata` or
  `gmail.readonly` on the DWD allowlist + a Workspace-admin step +
  one Gmail read per draft. Cheaper to defer until WS-B PR-4 stamps
  `thread_id` directly.
- **Drafts gated by severity window** — drafts don't notify, so
  off-hours noise is low; gating would be ceremony without
  meaningful spam reduction.
- **Single SA running everything** — letting `asb-routing-sa` be the
  DWD-grantable SA itself would avoid the `serviceAccountTokenCreator`
  binding. Rejected: ADR 0027 picked one SA on purpose. Letting
  multiple SAs be DWD targets means each one is an attack surface,
  and Workspace admins must remember which.

## Consequences

### Positive

- Critical client signals get a same-tick Chat ping AND a
  pre-drafted reply in the operator's Drafts folder.
- Existing scheduler/image/Job lifecycle stays intact — only env
  vars and a small TF binding change.
- `routed_events` schema needs no DDL — `channel` is free-text STRING
  per ADR 0025.

### Negative / risks

- **Gmail outage shares the Job execution with Chat.** Mitigation:
  per-channel try/except + `output.error_channels` aggregation.
- **Off-hours draft pile-up at high volume.** Mitigation: defer to
  follow-up ADR if volume is actually noisy.
- **Threading dormant until WS-B PR-4.** Acceptable: drafts work
  fine as fresh emails; threading is a later win.
- **Image cold-start grows.** `google-api-python-client` +
  `google-auth` already in Morning Brief image profile; tolerable.

### Migration

- PR 1 (this PR): code + tests + ADR. Plan-clean only.
- PR 2: TF — add the SA token-creator binding, add `TRIAGE_SA_EMAIL`
  + `GMAIL_DRAFT_RECIPIENT` env vars on `asb-routing-fanout` Job.
  Targeted apply.
- PR 3: image build + Cloud Run Job image swap + force-fire +
  end-to-end smoke (synthetic critical Gmail signal → Chat card +
  Gmail draft + 2 routed_events rows + 1 audit row).

## References

- ADR 0023 — WS-D Chat fan-out architecture
- ADR 0025 — `routed_events` insert-only table
- ADR 0027 — DWD delegation surface (drafts-only boundary)
- ADR 0029 — Morning Brief topology + DWD allowlist expansion
- `src/agency_brain/routing/channels/gmail.py` — adapter
- `src/agency_brain/common/dwd.py` — lifted DWD factory
- `src/agency_brain/routing/fanout.py` — `RoutingFanoutAgent`
