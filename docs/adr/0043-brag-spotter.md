# ADR 0043 — Brag Spotter (PKM Phase 3): Sunday weekly win-aggregator

**Status:** Accepted
**Date:** 2026-05-06
**Workstream:** WS-G PKM Phase 3

## Context

ADR 0037 (PKM merge architecture) sketched Phase 3 as: Sunday weekly
Cloud Run Job that scans the last 7 days across `triaged_items`,
`routed_events`, `notes`, `decisions`, and `evening_reflections` for
win signals; drafts rows into `agent_outputs.wins`; sends a
Sunday-evening Chat card and Gmail digest. The wins table schema is
already in place (`terraform/modules/agent_runtime/main.tf:703–744`,
ADR 0037 deliverable). Reflection v2 (ADR 0040) already populates wins
with `source_kind='reflection'` from voice memos. Phase 3 fills the
gap by sweeping wins from the *other four sources* that Reflection v2
doesn't see (or only sees indirectly via prose), and produces the
weekly digest the user reviews on Sunday evenings.

This is the second of three "weekly cadence" agents the PKM merge
calls for (Reflection daily, Brag Spotter weekly, Connector deferred
to Phase 5). The design must be cheap — the user expects ≪$1/week
runtime cost — and lean on existing scaffolding (Vertex SDK direct,
DWD impersonation, audit log emission).

## Decisions

### 1. Topology — single Cloud Run Job + weekly Cloud Scheduler, Vertex SDK direct

Mirror Morning Brief (ADR 0029) and Reflection v2 (ADR 0040):

- One Cloud Run Job `asb-brag-spotter` with `lifecycle.ignore_changes = [image]`.
- One Cloud Scheduler `asb-brag-spotter-weekly` firing Sunday 18:00 PT
  (after the day's reflection has run, before the user's Monday
  morning). Cron `0 18 * * 0` `America/Los_Angeles`.
- **Vertex SDK direct, NOT a Reasoning Engine** (per ADR 0028 / ADR
  0029 §3 / ADR 0033 §1). Avoids the orphan-RE cost incident posture.
  `google.genai.Client` with `vertexai=True` (the same SDK Reflection
  v2 uses for structured extraction per ADR 0040 §5).

Rejected: a separate Reasoning Engine. Rejected: a 5-minute polling
cadence (the week boundary is the load-bearing temporal unit; mid-week
re-runs would just re-hash the same source rows).

### 2. Service account — new `asb-brag-spotter-sa`, NOT reuse `asb-agent-triage-sa`

ADR 0027 §2's invariant is "one DWD-grantable SA"
(`asb-agent-triage-sa`). Other agents either run AS that SA (Morning
Brief, Reflection v2) or impersonate it for DWD scopes (Routing,
Risk Watcher). Brag Spotter doesn't need DWD — Gmail drafts are sent
via `asb-agent-triage-sa` impersonation (`gmail.compose` scope already
allowlisted, ADR 0027 + 0029).

`asb-brag-spotter-sa` (new SA) is the runtime; it carries:
- `bigquery.dataViewer` on `agent_outputs` + `airtable_replica` (read
  the 5 sources + existing wins for dedup)
- `bigquery.dataEditor` on `agent_outputs` (write `wins` + audit log)
- `bigquery.dataEditor` on `agent_audit_log` (BaseAgent contract)
- `iam.serviceAccountTokenCreator` on `asb-agent-triage-sa` (DWD
  impersonation, mirrors Routing's ADR 0032 §4 pattern)
- `secretmanager.secretAccessor` on `second-brain-gchat-webhook`
  (Chat dispatch)

`asb-brag-spotter-invoker-sa` holds only `run.invoker` (mirrors all
other agent invoker SAs).

Why a new runtime SA instead of reusing an existing one: principle
of least privilege — Brag Spotter doesn't need Triage Agent's writeback
PAT or Notes Ingestor's Drive scopes. Adding `bigquery.dataEditor` on
the union of needed datasets to an existing SA would bloat blast
radius. The SA cost is the audit boilerplate, not runtime fees.

### 3. Five readers, each over a 7-day window

| Source | Reader | Filter |
|---|---|---|
| `agent_outputs.triaged_items` | `RecentTriagedItemsReader` | `triaged_at >= NOW() - 7d` AND `actionable=TRUE` AND `severity IN ('critical','high','medium')` |
| `agent_outputs.routed_events` | `RecentRoutedEventsReader` | `routed_at >= NOW() - 7d` |
| `agent_outputs.notes` | `RecentNotesReader` | `ingested_at >= NOW() - 7d` AND `hipaa_isolated=FALSE` AND `extraction_method != 'failed'` |
| `agent_outputs.decisions` | `RecentDecisionsReader` | `decided_at >= NOW() - 7d` AND `status IN ('pending','reviewed_30','reviewed_90','reviewed_365')` (skips draft — those are unrefined) |
| `agent_outputs.evening_reflections` | `RecentReflectionsReader` | `generated_at >= NOW() - 7d` AND `mode = 'reflect'` (skips prompt mode — forward-looking, not retrospectable) |

Each reader returns an immutable dataclass list (`TriagedItemRow`,
`RoutedEventRow`, etc. — minimal projection, only the fields the
composer needs). Failures degrade gracefully: a missing source returns
`[]`, the section is elided from the LLM prompt, and the digest
composes from whatever returned.

Rejected: a 7-day-rolling lookback that crosses week boundaries
(would let a single win count toward two consecutive Sundays). Rejected:
JOINing `routed_events` to `triaged_items` to suppress the noisy
"every dispatch is a win" pattern — instead, the LLM prompt explicitly
instructs the model to filter routed_events for *novel* outcomes
(first-touch dispatches, milestone moments).

### 4. Synthesis — Vertex Gemini 2.5 Flash + `response_schema` (mirrors ADR 0040 §5)

The composer assembles a single prompt with all five source blocks and
asks Gemini for a structured JSON object: `{ commentary: str,
candidates: [{ title, summary, source_kind, source_id, evidence_links }] }`.
`commentary` is the digest body (Sunday-evening "this week" prose).
`candidates` are *new* wins to insert (deduped against existing wins —
see §5).

The schema constrains the LLM to emit `source_kind` from the same
enum as the table column (`triaged_item | routed_event | note | decision
| reflection`). `source_id` joins back to the originating row's PK.
`evidence_links` is a list of URLs lifted directly from source rows
(e.g., a triaged item's source_url). The LLM does not synthesize URLs.

`thinking_budget=0` (per ADR 0040 §5) — the prompt is structured;
hidden thinking eats output tokens.

### 5. Idempotency — dedup keys + week-existing pre-check

Two layers, both lifting the ADR 0040 §6 pattern:

- **Per-candidate dedup key:** `win_id =
  f"brag_spotter-{week_of}-{title_hash12(title)}"`. Keeps the
  re-fire-on-the-same-Sunday case clean: title normalization (lowercase
  / strip punct / collapse whitespace, lifted as
  `evening_reflection.extracted_writers.title_hash12`) means trivial
  LLM phrasing variation doesn't double-write. Pre-INSERT SELECT on
  `win_id` skips no-op rows.
- **Week-existing exclusion:** before invoking the LLM, the agent
  reads `agent_outputs.wins WHERE week_of = @monday` and passes the
  existing titles into the prompt as "already captured" context. The
  LLM is instructed to NOT re-emit candidates that semantically
  overlap. This reduces LLM cost (smaller candidate list) and keeps
  the digest cohesive (Reflection-extracted wins from earlier in the
  week aren't restated).

`week_of` is the ISO Monday of the run date (lifts
`evening_reflection.extracted_writers._monday_of_iso_week`).

### 6. Output channels — Chat card + Gmail draft (no Airtable)

Two adapters, lifted from existing patterns:

- **Chat card** via `second-brain-gchat-webhook` (Sunday-evening
  notification: "5 wins this week. View digest in Gmail draft.").
  Severity-aware in the future; v1 always fires.
- **Gmail draft** via `asb-agent-triage-sa` impersonation
  (`gmail.compose` DWD scope, no new scope per ADR 0027). Subject
  `Weekly wins — {week_of_human} ({n} wins)`. Body is the LLM
  `commentary` field, plus a bulleted list of all wins from the week
  (both reflection-extracted and brag-spotter-extracted), each with
  evidence links.

Rejected: writing the digest to Airtable Captures as a "win" record.
Captures is a one-way capture surface (form → BQ). Adding a
write-back direction violates ADR 0037 §4. Rejected: emitting a
`positive_goal_achieving` triage signal back through the routing
pipeline (would loop wins back into triage drafts; not the
right level for this).

### 7. Empty week — always fire the digest

If the week's source sweep produces zero candidates (and zero
reflection-extracted wins from earlier in the week), Brag Spotter still
sends a Chat card + Gmail draft with prose like "Quiet week — no wins
flagged. Worth pausing on what *did* go well that wasn't loud
enough to flag." Mirrors Morning Brief's "always-fire" posture (ADR
0029): the weekly ritual is load-bearing; silence implies the agent
broke.

### 8. HIPAA scope

All five readers filter HIPAA at the source:
- `triaged_items` — Triage Agent already excludes HIPAA accounts via
  the `airtable_replica` filterByFormula cascade (ADR 0011 →
  superseded by ADR 0020; HIPAA filter still applies via Account
  Lookup).
- `routed_events` — derived from non-HIPAA `triaged_items` /
  `risk_flags`.
- `notes` — explicit `hipaa_isolated=FALSE` filter.
- `decisions` — personal-only today (no `scope` column → personal,
  per ADR 0037).
- `evening_reflections` — personal-only today (mirrors decisions).

If `scope='agency'` rows ever land in `decisions` or
`evening_reflections`, add a `scope='personal' OR scope IS NULL`
filter to those two readers before flipping any work-context drafts
into the agency surface. Out of scope for this ADR.

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Reasoning Engine for synthesis | Same posture as ADR 0029 §3 — orphan-RE cost risk (ADR 0028) for a feature that doesn't need stateful tool-calling. |
| Daily Brag Spotter (vs weekly) | Wins are a weekly-cadence artifact — daily creates noise + restates ground reflection v2 already covers. |
| Vector-search-based win mining | Phase 5 Connector territory; the wins-shaped patterns we want are already structured in the source tables. Embedding similarity adds cost without signal at this corpus size. |
| One LLM call per source | 5x the calls + token spend for marginal quality lift; one call with all 5 blocks is cheaper and lets the model deduplicate cross-source. |
| Auto-promote draft decisions to "wins" | Decisions are predictions, not outcomes. The 90d retro is what closes the loop — auto-promoting a decision IS the win would be category-confused. |

## Consequences

- New TF: `asb-brag-spotter-sa` + custom role `tbBragSpotter` +
  `asb-brag-spotter-invoker-sa` + `google_cloud_run_v2_job` +
  `google_cloud_scheduler_job` + IAM bindings. ~150 LOC.
- New env-var passthrough `brag_spotter_image_tag` in
  `terraform/envs/prod/{variables.tf,main.tf,terraform.tfvars}`.
- New container image `asb-agents/brag-spotter` + `Dockerfile.brag-spotter`
  + `cloudbuild.brag-spotter.yaml`. Adds the 10th image to the PR-checks
  list (per CLAUDE.md "9 image builds" + this).
- New Python module `src/agency_brain/agents/brag_spotter/`
  (~700 LOC) + tests (~400 LOC).
- New ADR row in CLAUDE.md ADR list. `asb-brag-spotter` row in the
  "Live in GCP" table after rollout.
- Cost: Sunday weekly schedule × ~3000 tokens/run × $0.075/1M output
  ≈ $0.001/week. Negligible.
- HIPAA: all 5 readers filter at source per §8.

## Verification

Pre-merge:

1. `pytest -q tests/unit/agents/brag_spotter/` — all green
2. `terraform plan -target=module.agent_runtime.google_service_account.tb_brag_spotter_sa`
   (then incrementally for each new resource) — clean.
3. Render the LLM prompt against synthetic 5-source rows and confirm
   `response_schema` validates the response on a Vertex sandbox run
   (or an stubbed test).

Post-merge (image push + targeted apply):

1. Targeted apply (one resource at a time per the prod-touching
   workflow): `terraform apply -target=...tb_brag_spotter_sa` →
   custom role → invoker SA → IAM bindings → Job → scheduler.
2. `gcloud builds submit --config cloudbuild.brag-spotter.yaml --substitutions=_TAG=adr-0043-brag-spotter-v1`
3. `gcloud run jobs update asb-brag-spotter --image=...:adr-0043-brag-spotter-v1`
4. Force-fire: `gcloud scheduler jobs run asb-brag-spotter-weekly`.
   Verify (a) audit row in `agent_audit_log.events`
   (`agent_id="brag-spotter", success=true`), (b) new rows in
   `agent_outputs.wins` with `source_kind='brag_spotter'` (or zero
   if the week was empty + a null-digest audit row), (c) Chat card
   in `Brain alerts`, (d) Gmail draft in owner@example.com
   inbox. Re-fire and confirm idempotency (`win_id` SELECT skip).
5. Confirm scheduler unpaused for the next Sunday.

Cleanup smoke rows: `DELETE FROM agent_outputs.wins WHERE win_id LIKE 'brag_spotter-%' AND captured_at < CURRENT_TIMESTAMP() - INTERVAL 1 HOUR`.
