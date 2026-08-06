# ADR 0039 — PKM merge Phase 0b: Captures materializer + embeddings backfill

**Status:** Accepted
**Date:** 2026-05-06
**Workstream:** WS-G PKM Phase 0b (follows Phase 0a — ADRs 0037 + 0038)

## Context

Phase 0a (PRs #90 + #91, merged 2026-05-06) shipped the foundation
for the PKM merge — schema migrations, multi-MIME Notes Ingestor,
embeddings on write, IPARAG-adapted Drive layout, Captures Airtable
form schema. Two pieces were explicitly deferred to a follow-up PR
so the foundation could land in a reviewable chunk:

1. **Captures materializer.** The Airtable `Captures` form schema
   exists in `airtable/schema.json` (replicated automatically into
   `airtable_replica.captures` by the every-15-min sync), but no
   agent reads that replica and writes derived rows to
   `agent_outputs.{notes,decisions,wins}` or publishes to
   `asb-triage-input`. Without the materializer, form submissions
   stop at the replica table.

2. **Embeddings backfill.** Pre-ADR-0038 rows in
   `agent_outputs.notes` (those ingested before 2026-05-06) have
   `embedding IS NULL`. Phase 0a's idempotency guard skips re-embed
   when the SHA-256 content hash matches; the backfill needs a
   path to embed rows where the hash is still NULL.

This ADR scopes both pieces, plus the rollout sequence. It is
deliberately small — one Job + one env-var-gated branch in an
existing module — and the merged Phase 0a code already provides
all the BQ/IAM substrate.

## Decisions

### 1. Captures materializer = a new Cloud Run Job, NOT an extension of notes-ingestor

A separate Job + scheduler:

- **Cadence mismatch.** Notes-ingestor runs weekly (Mondays 6am PT
  per ADR 0031 §5). Captures form submissions need to flow into BQ
  within minutes — `*/15 * * * *` (matching `asb-airtable-sync-15m`)
  is the minimum acceptable.
- **Different IAM surface.** Captures needs Airtable WRITE access
  (flip `Synced`, then DELETE) — notes-ingestor SA only has Drive
  + Pub/Sub + BQ + Vertex, no Airtable. Either grant the
  notes-ingestor SA the Airtable write PAT (broadens its blast
  radius) or split into two SAs. Two SAs is the cleaner posture.
- **Different responsibility.** Notes-ingestor is "ingest from
  Drive into BQ + triage." Materializer is "promote a quick-capture
  Airtable row into the appropriate canonical table by `Kind`."
  Bolting them together blurs the responsibility line.

New resources:

- `asb-captures-materializer-sa` — runtime SA. Custom role
  `tbCapturesMaterializer`: BQ read on `airtable_replica.captures`,
  BQ write on `agent_outputs.{notes,decisions,wins}` + audit log,
  Pub/Sub publish on `asb-triage-input` (for `Kind=todo`),
  Secret Manager accessor on `airtable-tasks-write-pat-prod`. NO
  Vertex permission — embeddings are deferred to triage's RAG
  context loader (not part of materializer scope).
- `asb-captures-materializer-invoker` — invoker SA, `run.invoker`
  only. Mirrors `asb-morning-brief-invoker` shape per ADR 0029.
- Cloud Run Job `asb-captures-materializer` + Cloud Scheduler
  `asb-captures-materializer-15m` (`*/15 * * * *`, `Etc/UTC`).
- Image at `asb-agents/captures-materializer` (cleanup per ADR 0024
  inherits automatically).

PR-gate `scripts/sa_allowlist_check.py` gets two new entries
(both new SAs).

### 2. Kind dispatch: `note` → notes; `decision` → decisions; `win` → wins; `todo` → triage

Per the Captures form's `Kind` field (`airtable/schema.json`
Captures table):

- **`note` (default):** INSERT into `agent_outputs.notes` with
  `note_id = f"captures-{airtable_record_id}"` (deterministic
  dedup key),
  `note_kind = 'inbox'`,
  `scope = <Scope Hint>` (default `personal`),
  `markdown_content = <Note Text>`,
  `extraction_method = 'markdown-passthrough'`,
  `extraction_confidence = 1.0`,
  `embedding` populated via `text-embedding-005` (ADR 0038 §1).
  Then PUBLISH to `asb-triage-input` (kind-gated triage publish per
  ADR 0037 §6 — `inbox` kind always publishes).

- **`decision`:** INSERT into `agent_outputs.decisions` with
  `decision_id = uuid4()`,
  `decided_at = <Captured At>`,
  `title = <first line of Note Text, truncated to 80 chars>`,
  `context = <full Note Text>`,
  `choice = <Note Text>` (placeholder until refinement —
  the user fills in alternatives + prediction + confidence later
  via `/decide --refine` analog or BQ console),
  `review_30_at = DATE(Captured At) + 30 days`,
  `review_90_at = DATE(Captured At) + 90 days`,
  `review_365_at = DATE(Captured At) + 365 days`,
  `status = 'draft'`,
  `source_voice_note_id = NULL` (form-entered, not voice-extracted).

- **`win`:** INSERT into `agent_outputs.wins` with
  `win_id = uuid4()`,
  `captured_at = <Captured At>`,
  `week_of = <Monday of the ISO week of Captured At>`,
  `source_kind = 'manual'`,
  `source_id = <airtable_record_id>`,
  `title = <first line of Note Text, truncated to 80 chars>`,
  `summary = <Note Text>`,
  `evidence_links = []` (form has no link field; users can edit
  the row in BQ later).

- **`todo`:** PUBLISH directly to `asb-triage-input` Pub/Sub (no BQ
  write — triage will produce its own row when it classifies).
  Envelope mirrors how Drive notes publish per ADR 0031 §3:
  `source = 'airtable'` (existing `Source` enum),
  `source_event_ref = f"captures/{record_id}"`,
  `body = <Note Text>`,
  `subject = <first line, truncated 80>`,
  `aspects = ['captures_todo']` (new aspect tag, additive — no
  HIPAA implications because Captures is non-HIPAA by design;
  there's no HIPAA path on the form).

`note` is the default and most common path. The other three are
opinionated routing shortcuts so frequent capture types skip the
"note → triage classifies → human re-categorizes" loop. The user
can pick the right Kind when submitting.

### 3. Delete-after-sync guard: synchronous DELETE after BQ write, idempotent dedup as the safety net

Pragmatic v1: idempotency is the safety net, not a multi-tick
delay.

Per Airtable row in `airtable_replica.captures` where
`synced = FALSE`:

1. Materialize to BQ per §2 (using the deterministic dedup key
   from the airtable record id).
2. Mark Airtable row `Synced = TRUE`, `Synced At = NOW()` via the
   write PAT.
3. DELETE the Airtable row via the write PAT.

Failure modes:

- **BQ write fails** → no Synced flip, no DELETE; next tick
  retries. BQ writers are idempotent on the dedup key.
- **Synced flip succeeds, DELETE fails** → next tick reads the
  row again (still in `airtable_replica.captures` because not
  deleted; the next 15-min Airtable sync re-replicates it as long
  as it exists in Airtable). The BQ write is idempotent —
  pre-INSERT SELECT skips. Eventually consistent.
- **Synced flip fails after BQ write** → next tick reads, BQ
  write is idempotent, retries Synced + DELETE.
- **Materializer crashes mid-row** → row stays Synced=FALSE; next
  tick picks it up; BQ write idempotent.

The 15-min cadence + idempotent dedup gives a natural self-healing
window without explicit "wait N cycles" logic.

ADR 0037 §3's runbook entry mentioned "delete-after-sync confirmed
for ≥1 sync cycle" as a stronger guarantee. Deferring that to v1
because the idempotent dedup pattern is simpler and the failure
window is narrow (15 min worst case). Revisit if a real incident
shows up.

### 4. Embeddings backfill = env-var-gated branch in notes-ingestor `main.py`

NOT a separate Job. Reuses notes-ingestor's existing SA, IAM, and
image. New env var `BACKFILL_MODE` with values:

- `none` (default) — folder loop runs as ADR 0031 / 0037.
- `embeddings_only` — skip the folder loop entirely; instead
  query `agent_outputs.notes WHERE embedding IS NULL OR
  ARRAY_LENGTH(embedding) = 0`, embed each, UPDATE the row with
  `embedding`, `embedding_model`, `embedding_generated_at`,
  `embedding_content_hash`. Bounded by
  `MAX_BACKFILL_PER_TICK` (default 100) per execution.

UPDATE is allowed on `agent_outputs.notes` for rows past the
streaming-buffer window (~30 min after insert per ADR 0025 — DML
restriction on streaming buffer). Pre-ADR-0038 rows are days/weeks
old; buffer state is fine.

Trigger:

```bash
gcloud run jobs execute asb-notes-ingestor \
  --update-env-vars=BACKFILL_MODE=embeddings_only \
  --project=agency-brain-demo --region=us-central1
```

One-shot manual operation. Once the corpus is fully embedded, the
env var can be unset (or left at `none` via the next targeted
apply); the next scheduled tick runs the normal folder loop again.

`writer.py` gets a new `update_embedding(*, note_id,
revision_id, embedding, model, content_hash)` method that issues
a parameterized UPDATE. Audit row per UPDATE mirrors the existing
per-file audit shape with `decision: 'backfill_embedded'`.

### 5. Captures `Kind=todo` keeps the triage publish path uniform

The `todo` dispatch publishes directly to `asb-triage-input` rather
than writing to a new "todos" table — there isn't one, and we
don't need one. The existing Triage Agent pipeline handles
"actionable work" classification; manual todos from Captures form
are just one more signal source alongside Drive and Gmail.

The Triage Agent will produce its own `triaged_items` row from the
Pub/Sub envelope. That row's `source = 'airtable'`,
`source_event_ref = 'captures/{record_id}'` join key lets a future
query reconcile "what manual todo became what triage decision."

### 6. No new DWD scopes, no new managed services

Captures materializer reads from BQ, writes to BQ + Airtable +
Pub/Sub. None of those require DWD. ADR 0027 §2 invariant
preserved (one DWD-grantable SA — `asb-agent-triage-sa` — and the
materializer is not it).

`text-embedding-005` is invoked only from notes-ingestor (Phase 0a
+ backfill mode). Materializer's note-kind dispatch produces rows
that the next notes-ingestor tick *would* embed if they were in
Drive — but they're inserted directly via INSERT, so the
materializer needs to embed inline. **Updated:** materializer SA
DOES need `aiplatform.endpoints.predict` for the inline embed in
the `note` Kind path. Adding to `tbCapturesMaterializer` role.

(Caught during ADR drafting — initial design said "no Vertex" but
the `note` dispatch needs an embedding to satisfy ADR 0038 §3
"every notes row at write time." Better to embed at materialize
time than to leave NULL and require a second backfill pass.)

## Alternatives considered

- **Extend notes-ingestor instead of new Job.** Rejected (§1) —
  cadence, IAM, and responsibility mismatches.
- **Materialize via airtable-sync directly.** Rejected — sync is a
  generic per-table replicator (ADR 0010); per-table
  materialization logic doesn't belong there.
- **Backfill as separate Job.** Rejected (§4) — duplicates SA /
  IAM / image for a one-shot operation that mirrors the existing
  ingestor's embedder dependency exactly.
- **Multi-tick delete-after-sync.** Deferred (§3) — idempotency is
  enough for v1.
- **Separate `agent_outputs.todos` table for `todo` Kind.**
  Rejected (§5) — `asb-triage-input` is the existing
  actionable-signal queue; no need for a parallel one.
- **Inline embed via REST call from materializer to a "embeddings
  service" Cloud Run endpoint.** Overkill — Vertex SDK direct call
  matches the rest of the platform (Notes ingestor, Morning Brief,
  Risk Watcher all use direct Vertex calls).

## Consequences

**Positive**

- Captures form fully end-to-end: form submission → BQ canonical
  row + downstream channels within 15-30 min.
- Embeddings backfill cleanly closes the pre-ADR-0038 corpus gap.
- Notes-ingestor stays focused on Drive ingestion; new
  responsibilities go to a dedicated Job.
- All four Captures `Kind` values map to existing tables / queues
  — no new schema beyond what Phase 0a already landed.
- Idempotent dedup on a deterministic `airtable_record_id`-based
  key means re-runs are safe.

**Negative / accepted**

- One more Cloud Run Job in the platform
  (`asb-captures-materializer`). Adds ~96 scheduled invocations/day
  to platform tick volume. Minor.
- Two more SAs (`asb-captures-materializer-sa` + invoker). Mirrors
  the pattern of every other agent.
- Re-uses the existing `airtable-tasks-write-pat-prod` Secret —
  no new secret, but the secret's scope now widens conceptually
  (it was scoped to the Triage Agent's Tasks-write path; now
  also Captures materializer). Acceptable: the PAT's underlying
  Airtable scope already covers Captures because it was issued
  with `data.records:write` on the Operations base.

## Files / scope

**New:**
- `src/agency_brain/agents/captures_materializer/__init__.py`
- `src/agency_brain/agents/captures_materializer/main.py` — entrypoint, env-var loading, orchestration loop
- `src/agency_brain/agents/captures_materializer/agent.py` — `CapturesMaterializerAgent.invoke()`
- `src/agency_brain/agents/captures_materializer/readers.py` — `CapturesReader.read_unsynced()` from `airtable_replica.captures`
- `src/agency_brain/agents/captures_materializer/dispatch.py` — Kind → action mapping (note/decision/win/todo)
- `src/agency_brain/agents/captures_materializer/airtable_writer.py` — `flip_synced()`, `delete_capture()` via PAT
- `src/agency_brain/agents/captures_materializer/triage_publisher.py` — thin wrapper around `asb-triage-input` Pub/Sub
- `src/agency_brain/agents/captures_materializer/models.py` — Capture, MaterializeOutcome, IngestSummary
- `terraform/modules/agent_runtime/captures_materializer.tf` — SA + role + Job + scheduler + image var
- `Dockerfile.captures-materializer` — pip deps: bigquery, pubsub, pyairtable, vertexai
- `cloudbuild.captures-materializer.yaml` — image build config
- `tests/unit/agents/captures_materializer/{test_main,test_dispatch,test_airtable_writer,test_readers}.py`
- This ADR

**Modified:**
- `cloudbuild.yaml` — add captures-materializer image build step to PR gate
- `src/agency_brain/agents/notes_ingestor/main.py` — add `BACKFILL_MODE` env-var-gated branch; if `embeddings_only`, skip folder loop, run `_run_embeddings_backfill()` instead
- `src/agency_brain/agents/notes_ingestor/writer.py` — add `update_embedding(*, note_id, revision_id, embedding, model, content_hash)` method using parameterized UPDATE
- `tests/unit/agents/notes_ingestor/test_main.py` — add backfill-mode tests
- `tests/unit/agents/notes_ingestor/test_writer.py` — add `update_embedding` tests
- `terraform/modules/agent_runtime/notes_ingestor.tf` — add `BACKFILL_MODE` env var (default `none`), `MAX_BACKFILL_PER_TICK` (default 100)
- `terraform/envs/prod/{variables.tf,main.tf,terraform.tfvars}` — wire `captures_materializer_image_tag` (mirrors `notes_ingestor_image_tag` pattern from PR #87)
- `scripts/sa_allowlist_check.py` — add `asb-captures-materializer-sa` + invoker to ALLOWED_EMAILS
- `docs/runbooks/pkm-drive-layout.md` — add §Captures materializer with manual trigger + verification queries; add §Embeddings backfill with the one-shot trigger command
- `CLAUDE.md` + `AGENTS.md` — Tier 1 PKM merge entry: Phase 0b adds the Captures materializer and backfill in a separate PR after Phase 0a

## Verification

Pre-merge:
- `make lint && make test` — full suite green; new captures_materializer unit tests pass; backfill-mode tests pass.
- `terraform -chdir=terraform/envs/prod plan` against the branch — diff shows new captures-materializer resources only (no surprise drift on existing tables; explicitly verify `triaged_items` is not "must be replaced" — see `feedback_terraform_drift_check.md`).
- All 10 PR-checks gates green.

Post-merge (manual rollout):

1. Targeted apply:
   ```
   terraform apply \
     -target=module.agent_runtime.google_service_account.tb_captures_materializer_sa \
     -target=module.agent_runtime.google_service_account.tb_captures_materializer_invoker_sa \
     -target=module.agent_runtime.google_project_iam_custom_role.tb_captures_materializer \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_captures_materializer \
     -target=module.agent_runtime.google_cloud_scheduler_job.tb_captures_materializer_15m \
     -target=module.agent_runtime.google_cloud_run_v2_job.tb_notes_ingestor
   ```
2. User creates `Captures` Airtable table + form view per `docs/runbooks/pkm-drive-layout.md` §Airtable Captures form setup. Verify `airtable_replica.captures` lands within 15 min.
3. Build + push captures-materializer image:
   ```
   gcloud builds submit --config=cloudbuild.captures-materializer.yaml \
     --substitutions=_TAG=adr-0039-captures-materializer-v1 .
   ```
4. Update `terraform.tfvars` with new image tag; targeted apply.
5. Smoke fire materializer manually; verify all 4 Kind paths.

Smoke test 1 (note Kind):
- Submit Captures form `Kind=note`, `Scope Hint=personal`, body "test capture from phone."
- Wait ≤30 min.
- Verify row in `agent_outputs.notes` with `note_id LIKE 'captures-%'`, `note_kind='inbox'`, `scope='personal'`, `extraction_method='markdown-passthrough'`, `ARRAY_LENGTH(embedding)=768`.
- Verify Airtable Captures row deleted.
- Verify `agent_outputs.triaged_items` has a corresponding row with `source='drive'` and `source_event_ref` matching the captures pattern (Triage Agent's next 5-min tick after the materializer publishes to Pub/Sub).

Smoke test 2 (decision Kind):
- Submit `Kind=decision`, body "I decided to do X because Y."
- Verify row in `agent_outputs.decisions` with `status='draft'`, `review_30_at = DATE(Captured At) + 30`, `decided_at = Captured At`, `title` = first line truncated.
- Verify Airtable row deleted.

Smoke test 3 (win Kind):
- Submit `Kind=win`.
- Verify row in `agent_outputs.wins` with `week_of = Monday of submission week`, `source_kind='manual'`.
- Verify Airtable row deleted.

Smoke test 4 (todo Kind):
- Submit `Kind=todo`.
- Verify Pub/Sub publish to `asb-triage-input` (audit log shows materializer publish event).
- Verify Triage Agent classifies on next 5-min tick (`triaged_items` row with `source='airtable'`, `source_event_ref` matching `captures/{record_id}`).
- Verify Airtable row deleted.

Smoke test 5 (idempotency):
- Manually re-trigger the materializer immediately after a successful run. Verify no duplicate BQ rows (deterministic dedup key catches).

Backfill smoke:
- Trigger notes-ingestor with `BACKFILL_MODE=embeddings_only`.
- Verify per-row audit entries with `decision: 'backfill_embedded'`.
- Verify post-run:
  ```
  SELECT COUNTIF(embedding IS NULL OR ARRAY_LENGTH(embedding)=0) AS missing,
         COUNTIF(embedding_model IS NULL) AS missing_model,
         COUNT(*) AS total
  FROM `agency-brain-demo.agent_outputs.notes`
  ```
  Expect `missing = 0`, `missing_model = 0`.

## References

- ADR 0006 — BaseAgent audit contract
- ADR 0009 — `agent_outputs.*` schema design
- ADR 0010 — airtable-sync as a generic table replicator (rationale for keeping materialization in a separate Job)
- ADR 0024 — cost guardrails (730d BQ TTL, AR cleanup inherit automatically)
- ADR 0025 — insert-only on streaming-buffer rows; UPDATE allowed past buffer (backfill case)
- ADR 0027 — DWD allowlist (unchanged here)
- ADR 0029 — Morning Brief topology (Cloud Run Job + scheduler + invoker SA pattern this Job mirrors)
- ADR 0031 — Notes ingestor (parallel infrastructure; backfill code lives there)
- ADR 0037 — PKM merge architecture (this is Phase 0b of that rollout; Phase 1+ now ADR 0040+)
- ADR 0038 — Embeddings + `VECTOR_SEARCH` (backfill mechanics)
- `feedback_terraform_drift_check.md` — verify no `must be replaced` surprises before applying
