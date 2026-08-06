# ADR 0036 — Evening Reflection Composer topology

**Status:** Superseded by ADR 0040 (2026-05-06)
**Date:** 2026-05-05
**Workstream:** WS-G4 (Evening Reflection)

> **Superseded.** ADR 0040 (PKM Phase 1) replaces this topology with a
> two-mode (`prompt` / `reflect`) runtime that adds voice memo
> extraction. The single 18:00 PT scheduler defined here is renamed to
> `asb-evening-reflect-daily` and shifted to 21:00 PT in PR-C of ADR
> 0040; a new 16:00 PT `asb-evening-prompt-daily` scheduler invokes the
> same Job in prompt mode. The `prompt_version="v1"` template
> (`prompts/evening_reflection/v1.md`) is deprecated in favor of
> `reflect_v1.md`. Kept for historical context.

## Context

PRD §5.4 Track 2 names WS-G4 "Evening Reflection Composer" alongside WS-G3
Morning Brief, and PRD §V1 launch criterion #2 puts "≥14 days of daily
briefs and evening reflections" on the v1 ship checklist. The
proactive-agency spec at `docs/source/proactive_agency_brain_spec.md`
§5.5 (lines 358–372) defines Evening Reflection as a **backward-looking,
high-precision artifact** with three sections of prose — *what happened
today*, *what it might mean*, *worth carrying into tomorrow* — that
"makes the operator think, not generate noise" and is honest about
unremarkable days rather than padding.

Functionally it's a parallel deployment to Morning Brief: same SA, same
DWD scopes, same dedup pattern, same image-and-scheduler topology. The
content shape and prompt are different; the plumbing is not. This ADR
pins the topology so PR-A (this ADR + agent code + new BQ table) and
PR-B (TF wiring) can land cleanly.

ADR 0029 already established the daily-brief topology pattern. Most of
this ADR is "see ADR 0029" — the few decisions that *are* new (data
sources, sent-mail deferral, distinct image/scheduler) are spelled out
below.

## Decisions

### 1. Reuse `asb-agent-triage-sa` and existing DWD scopes

Per ADR 0027 §2 + ADR 0029 §1: one DWD-grantable SA, one delegation
surface. Evening Reflection runs as `asb-agent-triage-sa`, sharing the
existing `{gmail.compose, calendar.readonly}` allowlist. **No new
scope, no superseding ADR for the DWD surface.** Both scopes the
agent needs already serve Morning Brief.

The drafts-only PRD §4.7 boundary is preserved: Calendar is read-only,
Gmail is compose-only. The doc-driven `drafts_boundary_check.py` runtime
audit and the `drafts_static_check.py` PR-gate continue to enforce the
allowlist as-is.

### 2. Vertex SDK direct, not a Reasoning Engine

Same posture as ADR 0029 §3 — driven by the orphan-RE cost incident
on 2026-05-02 and codified in ADR 0028's `CreateReasoningEngine` alert.
One Cloud Run Job execution = one `vertexai.GenerativeModel.generate_content`
call. The shared composer surface in `agents/morning_brief/composer.py`
is the reference implementation; Evening Reflection has its own composer
with a different prompt template but the same SDK shape.

### 3. Topology — distinct Cloud Run Job + daily 18:00 PT scheduler

PR-B will land:

- Cloud Run Job `asb-evening-reflection` (deletion-protected; one-shot per
  scheduler tick; `asb-agent-triage-sa` as the runtime SA).
- Cloud Scheduler `asb-evening-reflection-daily` at `0 18 * * *` with
  `time_zone="America/Los_Angeles"`. Cloud Scheduler handles DST.
- Invoker SA `asb-evening-reflection-invoker-sa` with `run.invoker` only.
- Image at `us-central1-docker.pkg.dev/.../asb-agents/evening-reflection`,
  tag wired through env/prod tfvars (mirrors the `risk_watcher_image_tag`
  pattern from PR #87).

The spec says "5 minutes before evening processing time (usually 5:30
PM)" — the user-locked cadence is 6:00 PM PT (`0 18 * * *`). 5:30 PM
remains an option if the daily ritual benefits from a quieter slot.

### 4. Per-recipient-per-local-date dedup via SELECT

Mirrors ADR 0029 §4. The writer pre-checks
`agent_outputs.evening_reflections` for an existing row matching
`(recipient_email, local_date)` with `success = TRUE` and
`gmail_draft_id IS NOT NULL`. Hit → skip Gmail draft + skip BQ insert,
mark `dedup_skipped=true` on the audit row. Miss → proceed.

`success=FALSE` or `gmail_draft_id IS NULL` rows do **not** suppress a
retry (same as ADR 0029 — failure markers must not block re-attempts).

### 5. New BQ table `agent_outputs.evening_reflections`

Schema mirrors `agent_outputs.morning_briefs` field-for-field with
`reflection_id` as the PK. Cluster on `(recipient_email, local_date)`,
partition on `generated_at` with the standard 730-day TTL per ADR 0024.
Keeping the schemas symmetric makes per-day "did the daily ritual happen
on both ends?" queries trivial:

```sql
SELECT mb.local_date, mb.brief_id, er.reflection_id
FROM `agent_outputs.morning_briefs` mb
LEFT JOIN `agent_outputs.evening_reflections` er
  ON er.recipient_email = mb.recipient_email
  AND er.local_date = mb.local_date
WHERE mb.local_date >= CURRENT_DATE() - INTERVAL 14 DAY
```

### 6. v1 readers — five sources, sent-mail deferred to v2

The composer reads from five sources, each with graceful per-reader
degradation (one failed reader doesn't kill the brief — same posture as
ADR 0029 §_run):

1. **Tasks completed today** — `airtable_replica.tasks WHERE status='Done' AND completed_date = @local_date`.
2. **Triaged items today** — `agent_outputs.triaged_items WHERE DATE(triaged_at, @tz) = @local_date`. All severities, not just critical/high — the morning brief filtered noise; reflection includes everything that came through.
3. **Calendar attended** — Reuse `morning_brief.calendar_client.CalendarClient.events_for_today` directly. Same DWD `calendar.readonly` impersonation as Morning Brief.
4. **Today's morning brief plan** — `agent_outputs.morning_briefs WHERE recipient_email=@email AND local_date=@today`. Surfaces the morning's plan so the composer can compare *plan vs. execution* in the "what happened" section.
5. **Active risk flags** — `agent_outputs.risk_flags WHERE DATE(flagged_at, @tz) = @local_date AND resolved_at IS NULL`. New surfaces from today's 06:00 PT Risk Watcher tick.

Spec §5.5 also lists "new commitments made today (extracted from sent
mail)" — that requires a new `gmail.readonly` DWD scope (a superseding
ADR over 0027/0029, a Workspace Admin step, an allowlist update on
`drafts_boundary_check.py`). **v1 ships without it.** The spec's prompt
explicitly licenses honest "today was unremarkable" output, so the
deferral doesn't compromise the artifact's value. v2 adds the reader as
a follow-up ADR when the activation cost is justified by user feedback.

### 7. "Honest unremarkable day" fallback (not "Quiet day")

ADR 0029 §5 chose "always draft, even on quiet days" with a one-line
"Quiet day" body. Evening Reflection's tone is different — it's
reflective, not a checklist. The spec dictates:

> If you have no genuine insight to offer, say "today was a normal day,
> here's what got done" and stop.

The composer's empty-LLM-response fallback emits a one-paragraph
"unremarkable day" prose block (not a one-liner). Same operational
behavior — always draft, never skip — but tonally aligned with the
artifact's intent.

### 8. Single recipient for v1

`REFLECTION_RECIPIENTS` env var = `owner@example.com`.
Multi-recipient is deferred to a superseding ADR once the artifact
shape is settled and a second human (the partner per PRD §V1
criterion #2) is on board.

### 9. Direct import of Calendar + Gmail clients from `morning_brief/`

`evening_reflection/main.py` imports `CalendarClient` and
`GmailDraftsClient` directly from
`agency_brain.agents.morning_brief.{calendar_client,gmail_drafts_client}`
rather than lifting them to `common/`. Rationale: only two callers
today; lifting prematurely is enterprise ceremony for a 2-person
tool. Lift becomes worth doing when WS-G5 (Weekly Synthesizer) lands
as the third caller — that's the natural moment, not now.

## Consequences

**Positive**

- v1 launch criterion #2 unblocked on the evening-side. The
  morning–evening symmetry makes the daily-ritual reporting trivial.
- DWD surface untouched: no new scopes, no Workspace step, no
  allowlist update, no PR-gate change. Reuses ADR 0027/0029 plumbing
  end-to-end.
- Schema symmetry with `morning_briefs` simplifies analytics for the
  "did both run?" check.

**Negative / accepted**

- Marginal cost: one extra `gemini-2.5-flash` call per recipient per
  day (~$0.001/recipient/day) and one extra Cloud Run Job execution
  (~$0.0002/run). Negligible at the 2-person scale.
- One extra BQ partition (`agent_outputs.evening_reflections`,
  730d TTL).
- One extra image on AR (`asb-agents/evening-reflection`), under the
  recent-5 + 90d cleanup policy from ADR 0024.
- Sent-mail commitments reader missing in v1 — operationally that
  means the "what happened" section may underweight new commitments
  that didn't already produce a triaged item. Tracked as a v2
  follow-up.

## Rollout

### PR-A (this ADR + code + new BQ table)

Files in PR-A:
- `docs/adr/0036-evening-reflection.md` (this ADR).
- `src/agency_brain/agents/evening_reflection/` (full module).
- `src/agency_brain/prompts/evening_reflection/v1.md`.
- `terraform/modules/agent_runtime/main.tf` (add
  `agent_outputs.evening_reflections` table only — Cloud Run Job
  lands in PR-B).
- `Dockerfile.evening-reflection`.
- `cloudbuild.evening-reflection.yaml`.
- `cloudbuild.yaml` (add an `evening-reflection` image build step
  to the PR gate).
- `tests/unit/agents/evening_reflection/` (full parallel test
  suite).

Post-merge:
1. `terraform apply -target=module.agent_runtime.google_bigquery_table.evening_reflections`.
2. `bq show --schema agency-brain-demo:agent_outputs.evening_reflections` confirms the table.

### PR-B (TF wiring + image build + scheduler)

Lands after PR-A merges + applies. Cloud Run Job + scheduler + invoker
SA + first image build + scheduler-paused smoke + unpause. Mirrors
the ADR 0029 PR-B sequence.

Files in PR-B:
- `terraform/modules/agent_runtime/evening_reflection.tf`.
- `terraform/envs/prod/{variables.tf,main.tf,terraform.tfvars}` (wire
  `evening_reflection_image_tag` through, mirrors `risk_watcher_image_tag`
  pattern from PR #87 Item 2).

## References

- PRD §5.4 Track 2 (workstream decomposition); §V1 launch criterion #2
  (`docs/source/proactive_agency_brain_spec.md:484`); §4.7 (drafts-only)
- `docs/source/proactive_agency_brain_spec.md` §5.5 (Evening Reflection
  spec, lines 358–372); §11 (agent prompt template, lines 1026–1048)
- ADR 0006 (BaseAgent audit contract)
- ADR 0024 (cost guardrails — 730d BQ TTL inherits automatically)
- ADR 0026 (dedup-via-SELECT pattern reused)
- ADR 0027 (DWD delegation surface — this ADR adds a *consumer* of the
  existing allowlist; it does not modify the allowlist)
- ADR 0028 (CreateReasoningEngine alert — Evening Reflection uses
  Vertex SDK direct, not RE)
- ADR 0029 (Morning Brief topology — this ADR mirrors that topology)
