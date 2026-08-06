# ADR 0041 — Decisions Reviewer: routing fan-out third polling source

**Status:** Accepted
**Date:** 2026-05-06
**Workstream:** WS-G PKM Phase 2

## Context

PKM Phase 0b (ADR 0039) and Phase 1 (ADR 0040) both write rows into
`agent_outputs.decisions`. Captures Materializer dispatches `Kind=decision`
form entries every 15 min as `captures-decision-{record_id}` rows;
Evening Reflection v2 (`MODE=reflect`, 21:00 PT) extracts decisions
from voice memos as `reflection-{voice_note_id|reflection_id}-{title_hash12}`
rows. Both writers leave `alternatives=[]`, `prediction=NULL`,
`confidence=NULL` and set `status='draft'` — the schema's
`status='draft' → 'pending' → 'reviewed_30/90/365'` lifecycle (per
ADR 0037) starts here.

A draft sitting at `status='draft'` is invisible until the user
deliberately looks at the table. The retrospectives at +30/+90/+365
days that justify the schema in the first place can't fire until the
draft has a 90-day prediction and a confidence score. Phase 2's job
is to **surface drafts so the user can refine them** (fill in
alternatives + prediction + confidence + flip to `pending`). Phase 3
(Brag Spotter) and the eventual retrospective Job consume the
`pending`/`reviewed_*` rows downstream.

## Decisions

### 1. Routing fan-out third polling source — NOT a separate Cloud Run Job

The CLAUDE.md sketch envisioned a new `asb-decisions-reviewer` Cloud
Run Job at `0 8 * * *` PT. On inspection, that's enterprise ceremony
for notify-only behavior. The existing `asb-routing-fanout` is a
generic "poll BQ → dispatch via Chat + Gmail with `routed_events`
dedup" engine; risk_flags slotted in cleanly via ADR 0033 PR-C, and
decisions slot in the same way.

Phase 2 adds:

- `routing.polling.build_decisions_poll_query` — SELECT from
  `agent_outputs.decisions WHERE status='draft'` with a 24h lookback,
  channel-aware dedup via `routed_events.item_id = decision_id`. No
  severity column on `decisions` → no severity filter at the SQL
  layer; severity is synthesized in the row converter (see §2).
- `routing.fanout_main.decision_row_to_input` — maps a decisions row
  to `FanoutInput`; closes over the BQ project_id so the Gmail body's
  paste-ready UPDATE template references the FQN
  `{project}.agent_outputs.decisions`.
- A third tick in `routing.fanout_main.main()` after the risk_flags
  pass. No changes to `RoutingFanoutAgent`, channel adapter
  registration, or per-channel try/except.

No new Cloud Run Job. No new SA. No new scheduler. No new image. No
new IAM (`asb-routing-sa` already has `roles/bigquery.dataEditor` on
`agent_outputs` from ADR 0023 — covers reading `decisions` and
writing `routed_events` for `item_id=decision_id`).

### 2. Synthesize `severity='high'` so the daily-digest UX falls out of existing windowing

Decisions don't have severity. The row converter sets
`severity='high'`, which has two desirable side effects:

- **Chat 09:00–16:00 PT window applies** (ADR 0023's `same_day_if_before_4pm`
  cadence on `MORNING_BRIEF`/Chat-high). A draft created at 21:00 via
  Reflection v2 sits dormant until the next 09:00 PT routing tick
  fires the Chat card — naturally batching multiple drafts written
  overnight into a single morning notification, without a daily Job.
- **Gmail drafts always fire** (per ADR 0032, drafts don't notify).
  The user's inbox accumulates draft refine-prompts as decisions land,
  reviewable on their schedule.

This is the "daily 8am PT digest" UX the original CLAUDE.md sketch
called out — except it falls out of the existing
`fanout._channel_window_allows` logic without a separate scheduler.

### 3. Specialized formatters with paste-ready UPDATE template

Both formatters branch on `source == "decisions_reviewer"`:

- **Chat:** title + truncated context preview + a hint pointing at
  the Gmail draft for the actual UPDATE template. Keeps the Chat
  card terse — it's a notification, not a UI for editing.
- **Gmail draft:** subject `[DECISION DRAFT] {title}`. Body includes
  title, full context, current choice, an origin label
  (`captures form` vs `voice memo` derived from the decision_id
  prefix), the source voice memo note_id when present, and a
  copy-paste BQ console UPDATE template. The user fills three blanks
  (`alternatives`, `prediction`, `confidence`), pastes into BQ
  console, hits run; refinement is mechanical.

The template flips `status` to `pending` and stamps `refined_at` —
that lifecycle transition is what unblocks the future retrospective
Job (out of scope for ADR 0041).

### 4. Manual BQ console UPDATE for v1 (not Airtable, not gmail-reply parsing)

Two alternatives were rejected:

- **Airtable bidirectional sync** — adds a new sync direction
  (BQ → Airtable + Airtable → BQ), violating the single-source-of-
  truth model that today's Airtable replica relies on. Significant
  new infra not in scope for Phase 2; deferred to a later ADR if the
  manual UPDATE friction proves worth resolving.
- **Reply-to-Gmail-draft semantic parsing** — would need
  `gmail.readonly` (deferred per ADR 0027 / 0036) and structured-
  reply parsing logic. Novel surface for a notify-only feature.

Manual UPDATE is friction-y on purpose: the human-in-the-loop is
load-bearing for decision quality. If the friction is too high in
practice, the unmaintained-drafts count tells us so.

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Separate `asb-decisions-reviewer` Cloud Run Job + 08:00 PT scheduler | Duplicates infra (SA, scheduler, image, Dockerfile, cloudbuild config) for notify-only behavior. The routing fan-out already polls BQ + dispatches via channels with dedup. |
| Per-decision Chat severity (e.g. derive from decision content) | LLM call adds latency + cost for no signal — drafts are uniformly worth surfacing (the user wrote them down deliberately). Synthesized `'high'` keeps the Chat windowing predictable. |
| Auto-flip `status='draft' → 'pending'` on first dispatch | The status transition gates retrospectives; it has to mean "human refined this." Auto-flipping would silently fire 30/90/365-day retros against unrefined drafts. |
| Drop the Chat dispatch (Gmail-only) | Loses the morning visibility nudge; drafts in Gmail are easy to ignore for days. Chat gives the gentle "you have decisions to refine" reminder once per working morning. |

## Consequences

- `asb-routing-sa` reach unchanged (already has `agent_outputs`
  dataEditor); no IAM PR.
- New env var `DECISIONS_LOOKBACK_MINUTES` (default `1440`) on the
  `asb-routing-fanout` Cloud Run Job. Inlined as a literal in TF; if
  tunability proves useful, lift to `var.routing_decisions_lookback_minutes`
  later.
- `routed_events.item_id` continues to serve as a generic source-id
  column. No DDL change.
- Refinement remains friction-y in v1. That's the point.
- HIPAA scope: decisions are personal-only today (per ADR 0037, no
  `scope` column → personal). If work-context decisions land in this
  table later, add a `scope` column and wire the HIPAA filter into
  `build_decisions_poll_query` before flipping any work-scope drafts
  to `status='draft'`. Out of scope for this ADR.

## Verification

Pre-merge:

1. `pytest -q tests/unit/routing/` — all green
2. `terraform plan -target=module.agent_runtime.google_cloud_run_v2_job.tb_routing_fanout`
   — clean, only env-var diff
3. Render `format_gmail_draft` against a synthetic draft row and
   confirm the UPDATE template is paste-ready

Post-merge (image push + targeted apply):

1. Targeted apply: `terraform apply -target=...tb_routing_fanout`
2. `gcloud builds submit --config cloudbuild.routing-fanout.yaml --substitutions=_TAG=adr-0041-decisions-reviewer-v1`
3. `gcloud run jobs update asb-routing-fanout --image=...:adr-0041-decisions-reviewer-v1`
4. Smoke seed: `INSERT` one synthetic draft into
   `agent_outputs.decisions`; force-fire the Job; verify a
   `gmail_draft` row appears in `routed_events` (and Chat row too if
   forced inside the 09:00–16:00 PT window). Re-fire and confirm
   idempotency. Cleanup the smoke row.
