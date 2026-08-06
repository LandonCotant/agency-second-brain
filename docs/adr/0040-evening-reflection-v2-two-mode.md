# ADR 0040 — Evening Reflection v2: two-mode (prompt + reflect) + voice memo extraction

**Status:** Accepted
**Date:** 2026-05-06
**Workstream:** WS-G PKM Phase 1 (supersedes ADR 0036)
**Supersedes:** ADR 0036

## Context

ADR 0036 shipped the Evening Reflection v1 — a single backward-looking
prose draft daily at 18:00 PT. It reads five sources (completed tasks,
triaged items, calendar attended, today's morning brief, active risk
flags) and produces a three-section reflection that lands in the
recipient's Gmail Drafts.

The PKM merge architecture (ADR 0037) re-frames Reflection as the
**closing-the-loop step** between voice memo capture (Phase 0a Notes
Ingestor → `agent_outputs.notes`) and structured agency-ops state
(`agent_outputs.{decisions, wins}` — Phase 0a tables). Voice memos are
the user's primary capture surface for thoughts that don't fit the
Captures form (a 90-second drive home, a walk between meetings); they
land in BQ as transcribed markdown but currently terminate there. The
reader hand-offs from `notes` to `decisions`/`wins` are precisely what
v1 doesn't do.

Phase 1 also separates two distinct daily moments:

- **~16:00 PT — Evening anchor (forward-looking).** A brief Gmail
  prompt before the workday closes, listing in-flight decisions and
  unresolved triage follow-ups, asking 1–3 questions to focus the last
  hours of the day. Coaching tone, not status.
- **~21:00 PT — Reflection + extraction (backward-looking).** Reads
  the day's five v1 sources plus today's voice memos. Composes prose
  as v1 did, AND (in PR-B) emits structured rows for decisions, wins,
  and todos directly into the canonical tables.

This ADR scopes the **two-mode topology** + **voice memo reader** +
**ADR 0036 supersession**. The structured-extraction wiring
(response_schema, decisions/wins INSERTs, idempotency keys) is detailed
here for context but lands in PR-B; this ADR's PR-A is code-only
foundation.

## Decisions

### 1. One Cloud Run Job, two Cloud Schedulers, `REFLECTION_MODE` env override

`asb-evening-reflection` (the existing v1 Job) stays as-is. PR-C adds a
second Cloud Scheduler (`asb-evening-prompt-daily` at `0 16 * * *`
Pacific) that invokes the same Job with
`containerOverrides.env.REFLECTION_MODE=prompt`. The existing scheduler
is renamed `asb-evening-reflect-daily`, shifted to `0 21 * * *` Pacific,
and its body sets `REFLECTION_MODE=reflect`.

Rejected: two separate Cloud Run Jobs. The two modes share ~70% of the
same surface (BQ readers, audit log, dedup writer, `_VertexComposeClient`,
SA, calendar/Gmail clients). Two Jobs would duplicate the entire
adapter scaffolding in `main.py`, two image build pipelines, two
Artifact Registry images, two invoker SAs — all to fork dispatch on a
string flag. ADR 0036 §3 chose a separate Job from Morning Brief
because they're materially different artifacts; v2's two modes are *the
same artifact in two stances*.

The scheduler rename is risky to do as TF destroy/create — the window
where no scheduler runs would silently skip a daily reflection. PR-C
includes a pre-apply `terraform state mv` step:

```
terraform state mv \
  module.agent_runtime.google_cloud_scheduler_job.tb_evening_reflection_daily \
  module.agent_runtime.google_cloud_scheduler_job.tb_evening_reflect_daily
```

then a normal targeted apply. Same posture as ADR 0035's routing-fanout
rename.

### 2. Voice memo reader = 24h rolling window, NOT local-date

`RecentVoiceMemosReader` filters
`ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)`
rather than `DATE(ingested_at, tz) = @local_date`. Three reasons:

1. **Capture cadence reality.** A memo recorded at 20:55 PT and
   ingested at 21:02 PT misses a 21:00 PT reflect tick under
   "today only" semantics (the tick fires before ingestion).
   24h rolling catches the tail.
2. **Tick timing drift tolerance.** The cron may fire at 21:00 ±60s,
   and the user may shift it to 21:30 or 22:00 later. 24h rolling
   means *exactly one* reflect tick covers each memo, deterministically,
   regardless of cadence shifts.
3. **Idempotency interaction.** Per §6 below, structured-extraction
   dedup keys are per-`voice_note_id`. Even if two reflect ticks
   happen to overlap a memo, the second is a no-op. The 24h window
   biases toward "always cover the memo at least once."

### 3. Voice memo discriminator = `extraction_method = 'gemini-2.5-flash-audio'`

NOT `note_kind = 'inbox'` (every captures-path note is `inbox`) and
NOT a folder filter (the runbook's `Brain/Inbox/Voice/` convention is
human-readable but not enforced at the BQ layer). Notes Ingestor
(ADR 0031) stamps `extraction_method` per ingest pipeline:

- `gemini-2.5-flash-audio` — voice memos
- `gemini-2.5-flash-doc-export` — Google Docs
- `markdown-passthrough` — text/markdown captures
- `gemini-2.5-flash-pdf` — PDFs

Filtering on `extraction_method` is the cleanest semantic match. Voice
memos are the ones the LLM should "listen to" for decision/win/todo
extraction; Docs and PDFs are reference material that should appear in
the prose narrative but not drive structured-row creation.

### 4. Active-goals reader deferred (no `agent_outputs.goals` table)

The forward-looking prompt mode would benefit from listing "active
goals" alongside in-flight decisions. ADR 0037+ does not declare an
`agent_outputs.goals` table; Goals exist only in
`airtable_replica.goals` (Airtable-canonical, ADR 0009) and are
unowned by any agent's writer. For v1 of prompt mode, the
`InFlightDecisionsReader` (`status IN ('draft', 'pending')`) acts as
the goals proxy. Same posture as ADR 0036 §6's deferred sent-mail
reader.

If the Airtable goals corpus grows in usage, a follow-up ADR can lift
a `GoalsReader` over the replica. Not blocking Phase 1 ship.

### 5. Single Gemini call per mode with `response_schema` (PR-B)

Reflect mode emits one Gemini call returning a structured payload:

```json
{
  "commentary": "string — 3-section prose body (replaces v1 narrative)",
  "decisions": [
    {"title": "...", "context": "...", "source_voice_note_id": "..."}
  ],
  "wins": [
    {"title": "...", "summary": "...", "source_voice_note_id": "..."}
  ],
  "todos": ["string — short todo body"]
}
```

All three arrays are nullable / non-required so empty days return
`commentary` only. The Gmail draft body = `commentary` + a
deterministically-templated section listing extracted rows so the
reviewer can spot-check provenance before any retro reads hit the
canonical tables.

Rejected: two LLM calls (one for prose, one for extraction). Doubles
cost + latency, and creates a consistency hazard where prose and
extracted rows could disagree on what happened today.

The schema shape ports the OpenAPI Schema Object subset already in
production at `src/agency_brain/agents/triage/vertex_classifier.py`
lines 30–86 + 153–170 (`thinking_config(thinking_budget=0)`,
`max_output_tokens=8192`). The `vertexai.GenerativeModel` SDK doesn't
support `response_schema` — PR-B introduces a second compose client
(`_VertexStructuredComposeClient`) using `google.genai.Client` (same
SDK as the Triage classifier). The existing `_VertexComposeClient`
stays for prompt mode (prose-only).

### 6. Idempotency keys for extracted rows (PR-B)

```
decision_id  = f"reflection-{voice_note_id or reflection_id}-{title_hash12}"
win_id       = f"reflection-{voice_note_id or reflection_id}-{title_hash12}"
todo source_event_ref = f"reflection-todo/{reflection_id}/{title_hash12}"
```

`title_hash12` = `sha256(normalize(title)).hexdigest()[:12]`,
where normalize = lowercase / collapse whitespace / strip punctuation /
truncate 64 chars before hashing. LLM output varies in trivial ways
(trailing period, capitalization, double-space) that human-entered
captures don't; normalization makes the key stable across re-ticks.

Pre-INSERT SELECT skip on `decision_id` / `win_id` (lift the
`_row_exists` helper from
`src/agency_brain/agents/captures_materializer/dispatch.py:486-511`).

For todos, the dedup happens downstream — the Triage Agent already
dedups on `source_event_ref` per ADR 0026, so emitting the same
envelope twice is a no-op.

### 7. Mode-aware dedup (PR-C)

PR-C adds a `mode` column to `agent_outputs.evening_reflections`
(`STRING`, NULLABLE — pre-PR-C rows are NULL = backfilled to `reflect`
at read time). The dedup pre-check extends to
`AND (mode = @mode OR (mode IS NULL AND @mode = 'reflect'))` so a
4pm prompt-mode tick doesn't dedup against the same day's 9pm
reflect-mode tick.

PR-A and PR-B do not depend on this; until the second scheduler
exists, one reflection per day = no collision. The dedup column
addition is purely a PR-C concern.

### 8. Provenance threading

Every extracted decision/win row carries:

- `source_reflection_id = reflection_id` (FK to `evening_reflections`)
- `agent_run_id = run_id` (matches `agent_audit_log.events.run_id`)
- `source_voice_note_id` — populated when the LLM emits it; the
  reflect prompt instructs the model to attach the originating
  `note_id` to each extracted row when applicable.

Without `source_voice_note_id`, a 30/90/365-day decision retrospective
has no way to find the voice memo that birthed the decision — the
provenance audit trail collapses to best-effort heuristic matching.
The schema column already exists on `agent_outputs.decisions` per
ADR 0037 (`source_voice_note_id STRING NULLABLE`).

### 9. DWD allowlist unchanged

Voice memo content is read from `agent_outputs.notes` (BQ) — that
doesn't touch DWD. Notes Ingestor's existing DWD scope set is
unaffected. ADR 0027 §2's "one DWD-grantable SA, two scopes
(`gmail.compose`, `calendar.readonly`)" invariant holds.

`drafts_boundary_check.py` runtime audit and `drafts_static_check.py`
PR-gate stay clean — no allowlist update, no Workspace Admin step.

### 10. Prompt mode reads tighter triage subset

The existing `TriagedItemsTodayReader` returns ALL severities (ADR
0036 §6). For prompt-mode "open followups," the reader is reused
unchanged but the rendering layer filters in-memory to
`action_type IN ('do_now', 'defer', 'schedule', 'wait')` — the
actionable subset that warrants the user's attention before EOD.
Pure-info items get rendered into the reflect-mode prose but not the
prompt-mode anchor questions.

This is a render-time filter (not SQL) so the same reader serves both
modes without a SQL fork. PR-A wires this filter in `composer.py`'s
new `_render_open_followups` helper (called by prompt-mode only).

## Alternatives considered

- **Two separate Cloud Run Jobs.** Rejected (§1) — duplicate
  scaffolding for a 70% shared surface.
- **Voice memo reader keyed on `note_kind = 'inbox'`.** Rejected (§3) —
  every captures-path note is `inbox`; doesn't isolate audio.
- **Folder-id filter on voice memos.** Rejected (§3) — `note_kind`
  is the post-extraction fact; folder ids are pre-extraction inputs
  that don't survive into BQ as a queryable column.
- **Two LLM calls (prose + extraction).** Rejected (§5) — cost,
  latency, consistency hazard.
- **Active goals reader from `airtable_replica.goals`.** Deferred
  (§4) — unowned today; lift only if usage justifies.
- **Mode column added in PR-A.** Rejected — keeps PR-A code-only;
  schema addition belongs with the second-scheduler TF apply (PR-C).

## Consequences

**Positive**

- Voice memo content stops dead-ending in `agent_outputs.notes` —
  the reflect-mode pass extracts decisions/wins/todos automatically,
  closing the PKM-merge loop (ADR 0037).
- Prompt mode is a low-friction experiment surface — if the 4pm
  anchor doesn't help, kill the scheduler and the Job remains.
- Mode separation lets us iterate on each prompt independently
  (`prompt_v1.md` vs `reflect_v1.md`) without churn on the other.
- DWD allowlist untouched, drafts-only boundary preserved, no
  Workspace Admin step.

**Negative / accepted**

- Two Gemini calls/recipient/day instead of one — ~$0.002/day at the
  2-person scale. Negligible against the $50/mo budget alert.
- One extra Cloud Scheduler (PR-C). Minor.
- The `_VertexStructuredComposeClient` (PR-B) introduces a second
  Vertex SDK pathway (`google.genai` alongside `vertexai`) — same
  pattern Triage classifier already uses, so no new surface area.
- ADR 0036 is officially superseded; its prompt template (`v1.md`)
  is kept but marked deprecated. The reflect-mode template
  (`reflect_v1.md`) is the new source of truth.

## Rollout

### PR-A (this ADR + foundation, code-only)

Files in PR-A:

- `docs/adr/0040-evening-reflection-v2-two-mode.md` (this ADR).
- `docs/adr/0036-evening-reflection.md` — add `Status: Superseded by
  ADR 0040 (2026-05-06)` header.
- `src/agency_brain/agents/evening_reflection/models.py` — add
  `ReflectionMode` enum, `VoiceMemo` + `InFlightDecision` dataclasses,
  `mode` field on `EveningReflectionInput` (default `REFLECT`).
- `src/agency_brain/agents/evening_reflection/readers.py` — add
  `RecentVoiceMemosReader` + `InFlightDecisionsReader`.
- `src/agency_brain/agents/evening_reflection/composer.py` — add
  `_render_voice_memos` + `_render_in_flight_decisions` +
  `_render_open_followups`; extend `render_section_blocks`.
- `src/agency_brain/agents/evening_reflection/agent.py` — accept
  optional voice-memo + in-flight-decision readers; branch `_run` on
  `input.mode`.
- `src/agency_brain/agents/evening_reflection/main.py` — read
  `REFLECTION_MODE` env; construct mode-appropriate readers + prompt
  template; set `mode` on input; differentiate subject line and
  `prompt_version`.
- `src/agency_brain/prompts/evening_reflection/prompt_v1.md` — new
  forward-looking 4pm anchor template.
- `src/agency_brain/prompts/evening_reflection/reflect_v1.md` — new
  9pm reflection template (prose-only in PR-A; structured-extraction
  block is added in PR-B at a marker comment).
- `src/agency_brain/prompts/evening_reflection/v1.md` — deprecated
  header.
- `tests/unit/agents/evening_reflection/{test_readers,test_models}.py`
  — extend with new reader + dataclass coverage.
- `tests/unit/agents/evening_reflection/test_main_mode_switch.py` —
  new file covering MODE dispatch.

PR-A is code-only — no TF, no image rebuild, no scheduler change.
The existing 21:00 PT scheduler keeps firing the existing Job; the
new code defaults to `REFLECTION_MODE=reflect` so behavior is
unchanged from v1 until PR-C deploys.

### PR-B (extraction wiring)

- `_VertexStructuredComposeClient` using `google.genai.Client`,
  `response_schema` per §5.
- `composer.py` extended with `compose_structured` returning the
  parsed payload; reflect-mode `agent.py` calls it.
- New writers: `DecisionsWriter` + `WinsWriter` (mirror
  `captures_materializer/dispatch.py`'s INSERT pattern with
  pre-INSERT SELECT skip).
- Reflect-mode todos publish to `asb-triage-input` Pub/Sub.
- `reflect_v1.md` gets the structured-extraction instruction block.
- Integration tests with stubbed `google.genai`.

### PR-C (TF + scheduler rename + mode column)

- `terraform state mv` for the existing scheduler, then rename in
  HCL.
- New scheduler `asb-evening-prompt-daily` at `0 16 * * *` PT.
- Existing scheduler `asb-evening-reflect-daily` at `0 21 * * *` PT
  with `containerOverrides.env.REFLECTION_MODE=reflect`.
- `ALTER TABLE agent_outputs.evening_reflections ADD COLUMN mode STRING`
  via the `bigquery_table` schema definition.
- Image rebuild + targeted apply + paused-smoke for both modes,
  then unpause.

## Verification

### Pre-merge (PR-A)

- `make lint && make test` — full suite green; new reader, model,
  and main-mode-switch unit tests pass.
- All 10 PR-checks gates pass.
- `terraform plan` clean (PR-A is code-only).

### Out of PR-A (covered by PR-B/C)

- Reflect-mode tick with a voice memo containing "I decided to do X"
  → `agent_outputs.decisions` row with `source_voice_note_id` set,
  `status='draft'`.
- Re-trigger reflect-mode same evening → dedup-skip on the
  `evening_reflections` row, no duplicate decision rows (idempotency
  key collision).
- 4pm prompt-mode tick → Gmail draft subject starts
  `"Evening Anchor"`, body lists in-flight decisions and
  actionable followups.

## PR-C closeout addendum (2026-05-06)

PR-C shipped TF for the second scheduler + mode column + cron shift.
Two deliberate deviations from the original §1 design call out below:

1. **Kept GCP scheduler name `asb-evening-reflection-daily` for the
   REFLECT scheduler (cosmetic rename deferred).** §1 anticipated
   renaming via `terraform state mv`; in practice the rename forces
   a destroy/create on Cloud Scheduler (the resource's `name` is
   immutable on the GCP side), which would create a window with no
   scheduler firing. The GCP name is internal — the resource serves
   the REFLECT mode regardless of its label. New `asb-evening-prompt-daily`
   resource carries the PROMPT mode. Cosmetic rename can land in a
   future PR if it provides any operational value.

2. **`mode` column lands inline as part of the existing schema
   `jsonencode([...])`, not via `ALTER TABLE`.** Per ADR 0026 / the
   `feedback_terraform_drift_check.md` invariant, `terraform plan`
   was verified to show an in-place schema update (no
   `must be replaced`) before merging. The dedup pre-check tolerates
   pre-PR-C rows where `mode IS NULL` by treating them as `reflect`
   (`mode = @mode OR (mode IS NULL AND @mode = 'reflect')`).

Image rebuild ships at tag `adr-0040-evening-reflection-v1` via
`cloudbuild.evening-reflection.yaml`; tfvars updated to that tag in
this PR. **`tfvars` is documentation of intent only** — the Cloud Run
Job has `lifecycle.ignore_changes = [image]` (mirrors morning-brief /
risk-watcher / notes-ingestor), so `terraform apply` will NOT redeploy
the image when only the var changes. The actual deploy happens via
`gcloud run jobs update --image=...` in step 3 below. Verified against
the `terraform plan` output: the Cloud Run Job did not appear in the
diff, only the BQ table + the two schedulers.

Post-merge rollout sequence:

1. **Build + push image:**
   ```
   gcloud builds submit \
     --project=agency-brain-demo \
     --region=us-central1 \
     --config=cloudbuild.evening-reflection.yaml \
     --substitutions=_TAG=adr-0040-evening-reflection-v1 \
     .
   ```
2. **Targeted plan** — confirm in-place updates only:
   ```
   terraform -chdir=terraform/envs/prod plan \
     -target=module.agent_runtime.google_bigquery_table.evening_reflections \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_evening_reflection \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_evening_reflection_daily \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_evening_prompt_daily
   ```
   Expected: `1 to add, 2 to change, 0 to destroy.` STOP if any
   `must be replaced` shows up (`feedback_terraform_drift_check.md`).
3. **Targeted apply** of the same:
   ```
   terraform -chdir=terraform/envs/prod apply \
     -target=module.agent_runtime.google_bigquery_table.evening_reflections \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_evening_reflection \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_evening_reflection_daily \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_evening_prompt_daily
   ```
4. **Deploy the new image** — required because of `ignore_changes = [image]`:
   ```
   gcloud run jobs update asb-evening-reflection \
     --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/evening-reflection:adr-0040-evening-reflection-v1 \
     --project=agency-brain-demo \
     --region=us-central1
   ```
   Without this step, force-fired ticks below would run against the
   previously-deployed image (e.g. `bootstrap`), which doesn't carry
   PR-A/B/C code.
5. **Smoke fire each mode:**
   ```
   gcloud run jobs execute asb-evening-reflection \
     --update-env-vars=REFLECTION_MODE=prompt \
     --project=agency-brain-demo --region=us-central1
   gcloud run jobs execute asb-evening-reflection \
     --update-env-vars=REFLECTION_MODE=reflect \
     --project=agency-brain-demo --region=us-central1
   ```
   Verify: prompt-mode produces a Gmail draft with subject
   `Evening Anchor — …`; reflect-mode produces an
   `Evening Reflection — …` draft AND lands one+ rows in
   `agent_outputs.decisions` and/or `agent_outputs.wins` (assuming
   the day's voice memos contained extractable content).
6. **Unpause both schedulers:**
   ```
   gcloud scheduler jobs resume asb-evening-reflection-daily \
     --location=us-central1 --project=agency-brain-demo
   gcloud scheduler jobs resume asb-evening-prompt-daily \
     --location=us-central1 --project=agency-brain-demo
   ```

## References

- ADR 0006 — BaseAgent audit contract
- ADR 0009 — `agent_outputs.*` schema design
- ADR 0026 — triage dedup (covers todo `source_event_ref` reuse) +
  the BQ-schema-change invariant referenced in this PR-C addendum
- ADR 0027 — DWD delegation surface (this ADR adds zero scopes)
- ADR 0029 — Morning Brief topology (PR-C scheduler pattern mirrors)
- ADR 0031 — Notes Ingestor (`extraction_method` source)
- ADR 0036 — Evening Reflection v1 (superseded by this ADR)
- ADR 0037 — PKM merge architecture (this is Phase 1 of that rollout)
- ADR 0038 — Embeddings + `VECTOR_SEARCH` (no direct dependency, but
  voice memos are embedded by Notes Ingestor — cross-references will
  be valuable for the Phase 5 Connector)
- ADR 0039 — Captures materializer (idempotency-key pattern reused;
  `_row_exists` helper lifted in PR-B)
- `src/agency_brain/agents/triage/vertex_classifier.py` —
  `response_schema` shape template (PR-B port target)
