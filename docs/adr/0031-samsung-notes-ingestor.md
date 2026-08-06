# ADR 0031 — Samsung Notes ingestor (Drive folder + Gemini multimodal)

**Status:** Accepted
**Date:** 2026-05-02
**Workstream:** WS-G (new ingestion path)

## Context

The user takes notes throughout the day on a Samsung Tab S9 in Samsung Notes
— a mix of typed text and raw S-Pen handwriting, sometimes annotated over
diagrams or screenshots. They want those notes to feed the Brain across four
use cases: (1) triage signal so actionable notes draft Airtable Tasks via the
existing Triage Agent, (2) Morning Brief context ("what you wrote yesterday"),
(3) a searchable corpus, (4) RAG context for future Triage classifications.

Samsung Notes has no public API and no native Google Drive sync — Samsung
Cloud and OneDrive are the only built-in cloud targets. The only viable
capture is the user's manual **Share → Save to Drive (PDF)** gesture, which
is one tap on the share sheet. This ADR pins the ingestion topology that
turns those PDFs into structured input for the existing pipeline.

The architectural fit is good. The existing Triage Pub/Sub envelope at
`src/agency_brain/agents/triage/bridge.py:306` already declares
`Source.DRIVE` (`agents/triage/models.py:18`), and BaseAgent's HIPAA
short-circuit keys off the `hipaa_excluded` aspect carried on `TriageInput`
(`agents/base.py`). A new ingestor can republish into `asb-triage-input` with
the appropriate aspects and the existing classifier handles the rest. No
cross-cutting changes to TriageInput, BaseAgent, or the bridge.

## Decisions

### 1. Capture flow: Drive folder share, not DWD

The user creates two folders in their personal Drive: `Brain Inbox/Notes/`
(default) and `Brain Inbox/Notes-HIPAA/`. They share both folders with
`asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com` as
**Editor** (the SA must move processed files to a `processed/` subfolder).
The SA accesses the folders via ADC under its own identity using
`drive.googleapis.com` — **not** via Domain-Wide Delegation.

This is intentional: ADR 0027's DWD allowlist (`gmail.compose`,
`calendar.readonly`) does not need to expand. Drive folder sharing is
OAuth-invisible; `docs/dwd_scopes.md` and `drafts_boundary_check.py` audits
remain clean. ADR 0027 §2 ("adding any other scope requires a superseding
ADR") is preserved untouched.

### 2. Single-model extraction with Vertex Gemini 2.5 Flash multimodal

The ingestor sends each PDF to `gemini-2.5-flash` via the Vertex SDK with a
prompt that asks for Markdown output with inline `[Image: caption]`
descriptions for graphics. One model handles three jobs that would otherwise
take two services: typed text, S-Pen handwriting OCR, and image captioning.

**Rejected alternatives:**

- **Document AI Document OCR** (~$1.50/1k pages). Doubles the IAM surface,
  the SA scope, and the dependency footprint for an unproven need. Doesn't
  do image captioning, so a multimodal model would still be required as a
  second step. Per-note cost is comparable.
- **pypdf-then-Document-AI hybrid.** Premature optimization for "typed-only"
  notes — the user's content profile is mixed and a uniform pipeline is
  simpler to operate. Saves cents per month at the price of branching
  logic.

`extraction_method` and `extraction_confidence` columns are persisted on the
BQ row so a future Document AI fallback can be added behind the existing
column without a schema migration if Gemini hallucination becomes a problem.

Vertex Gemini is HIPAA-eligible under Google's BAA and is the model the
existing Triage Reasoning Engine already runs against — no new compliance
posture.

### 3. HIPAA: folder convention → aspect on triage publish

Notes can contain anything; they have no Account/Project linkage at capture
time, so the existing HIPAA cascade (rooted at `Accounts.HIPAA` per ADR
0020) cannot apply automatically. Folder convention is the user-driven
guardrail:

- File in `Brain Inbox/Notes/`        → `aspects = ["samsung_note"]`
- File in `Brain Inbox/Notes-HIPAA/`  → `aspects = ["samsung_note", "hipaa_excluded"]`

The corpus row carries an explicit `hipaa_isolated` boolean derived from
the same folder convention. Phase 2 readers (Morning Brief notes section)
filter `hipaa_isolated = false` so HIPAA notes never bleed into a
non-HIPAA-context brief.

**All notes publish uniformly to `asb-triage-input`.** The `hipaa_excluded`
aspect on a HIPAA-folder note triggers `BaseAgent.invoke`'s pre-flight
check (`agents/base.py`), which short-circuits with a `HIPAA_GUARD_TRIPPED`
audit row (per ADR 0006) — the correct posture for HIPAA content. This is
preferred over branching on the ingestor side because it reuses the
project-wide HIPAA gate rather than carving out custom logic that would
diverge from how every other source handles HIPAA.

### 4. Dedup: stable `note_id` + per-revision row

`note_id = drive_file_id` (stable across edits) keys the corpus row.
`revision_id` from the Drive API is stored as a separate column. A re-edit
of the same note in Samsung Notes that the user re-shares creates a new
row keyed `(note_id, revision_id)` and a new triage classification —
the user *meant* to re-share with new content.

The existing 24h triage `input_hash` window (ADR 0026) catches true
redeliveries: the bridge passes `source_event_ref = f"{drive_file_id}#{revision_id}"`
on the publish; if a tick happens to re-list the same file before the move
to `processed/`, the body+source_event_ref combination produces an
identical input_hash and the triage writer skips. Pre-INSERT SELECT in the
ingestor's BQ writer also short-circuits on `(drive_file_id, revision_id)`
hits before any Vertex spend.

### 5. Cadence: weekly (Mondays 6am Pacific)

`0 13 * * 1` in `Etc/UTC` (Mondays at 13:00 UTC = 6am Pacific). One tick
per week drains the prior week's accumulated notes before Monday's
Morning Brief composes.

**Why not faster.** An earlier draft of this ADR proposed every 10 min
so a note shared at 7:20am made that morning's 7:25am brief. That
rationale collapsed once we accepted that **Gmail is the real-time
signal channel for the Triage Agent and notes are the slower
reflection corpus**. Time-critical signals (client emails, calendar
holds) flow through `asb-triage-input-sub` at 5-min cadence already; a
note saying "follow up with X" sitting up to 7 days before
classification is a non-issue when the urgent version of that signal
would have arrived via email anyway.

**Cost implication.** Vertex spend drops from ~144 ticks/day worst-
case to ~1 tick/week — roughly 1000× cheaper for the same total
volume. The trade-off is that use case 1 (triage signal on notes) has
up to 7 days of latency. For use cases 2 (Morning Brief context),
3 (corpus), and 4 (RAG retrieval), weekly is identical to faster
cadence in practice.

**Backlog headroom.** `MAX_NOTES_PER_TICK` defaulted up to 100 (from
20 in the every-10-min draft) so a week's worth of notes drains in
one tick without leaving a residual queue.

### 6. Watermark + cost cap

Per-folder `last_modified_time_seen` row in a new
`agent_state.notes_ingestor_watermark` table; Drive `list` uses
`q=modifiedTime > '<watermark>' and trashed=false and mimeType='application/pdf'`
to keep page sizes flat. `MAX_NOTES_PER_TICK=20` env var bounds the
worst case if the user bulk-shares a backlog (mirrors `MAX_MESSAGES=50`
on the triage bridge).

### 7. Cloud Run Job, not Reasoning Engine

Mirrors ADR 0029 §3. One Cloud Run Job execution per scheduler tick
calls Vertex directly via the SDK; ADR 0028's `CreateReasoningEngine`
alert never fires for Notes ingestion, and the orphan-RE cost incident
posture (2026-05-02) does not apply.

### 8. Phasing

This ADR scopes **Phase 1**: ingestion + corpus + triage publish. Phases
2 (Morning Brief notes section) and 3 (Notes context loader for triage
RAG) are explicit follow-ups that pattern-copy cheaply once the corpus
exists.

## Consequences

**Positive**

- Use case 1 (triage signal) and use case 3 (corpus) are live on Day 1.
- DWD allowlist unchanged → ADR 0027 audit posture preserved.
- HIPAA cascade reuses existing BaseAgent gate; no fork.
- Future Notes RAG (use case 4) and Morning Brief surfacing (use case 2)
  are reader-only follow-ups.

**Negative / accepted**

- Manual Share gesture per note. Acceptable trade-off for not building
  device-side automation on a 2-person tool.
- Vertex Gemini cost: ~$0.001/note. Capped by `MAX_NOTES_PER_TICK=20`.
- One extra image on AR (`asb-agents/notes-ingestor`), under the recent-5
  + 90d cleanup policy from ADR 0024.
- Two extra BQ tables (`agent_outputs.notes` 730d TTL,
  `agent_state.notes_ingestor_watermark` no TTL — ~2 rows total).
- Drive Folder Editor grant on the SA: blast radius is the two notes
  folders (and the `processed/` subfolders the SA creates inside them).
  Documented in `docs/runbooks/notes-ingestor.md`.

## Rollout

1. Land this ADR + ingestor module + Terraform + tests in one PR.
2. Targeted apply per `feedback_prod_touching_workflow.md`:
   `terraform apply -target=module.agent_runtime.google_bigquery_table.notes`
   `-target=module.agent_runtime.google_bigquery_table.notes_ingestor_watermark`
   `-target=module.agent_runtime.google_service_account.tb_notes_ingestor_sa`
   `-target=module.agent_runtime.google_project_iam_custom_role.tb_notes_ingestor`
   `-target=module.agent_runtime.google_cloud_run_v2_job.tb_notes_ingestor`
   `-target=module.agent_runtime.google_cloud_scheduler_job.tb_notes_ingestor_10m`
3. Build + push first image:
   `gcloud builds submit --config=cloudbuild.notes-ingestor.yaml --substitutions=_TAG=adr-0031-notes-ingestor-v1 .`
4. `gcloud run jobs update asb-notes-ingestor --image=us-central1-docker.pkg.dev/.../asb-agents/notes-ingestor:adr-0031-notes-ingestor-v1`
5. **Out-of-band step (user):** Create `Brain Inbox/Notes/` and
   `Brain Inbox/Notes-HIPAA/` in personal Drive; share both with
   `asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com`
   as Editor. Verify by running the job manually:
   `gcloud run jobs execute asb-notes-ingestor`.
6. Smoke (see plan §Verification).

## References

- ADR 0006 (BaseAgent audit contract; HIPAA short-circuit pattern)
- ADR 0009 (`agent_outputs` schema design)
- ADR 0019 (Cloud Run Job + scheduler topology — bridge pattern)
- ADR 0020 (single Operations Airtable base; HIPAA cascade root)
- ADR 0024 (cost guardrails — 730d BQ TTL inherits automatically)
- ADR 0025 (insert-only, no DML on streaming-buffer rows)
- ADR 0026 (input_hash dedup window; reused on the triage publish leg)
- ADR 0027 (DWD delegation surface — preserved untouched)
- ADR 0028 (CreateReasoningEngine alert — Notes uses Vertex SDK direct)
- ADR 0029 (Morning Brief topology — pattern this ADR mirrors)

## Closeout addendum — 2026-05-04 first weekly run

The first scheduled run (Mon 6am Pacific) ingested only 1 of the
expected several Samsung Notes PDFs. Diagnosis: Drive's `q=` filter
used `mimeType = 'application/pdf'`, but Samsung Notes' "Save to
Drive (PDF)" sometimes uploads with a non-standard mimeType
(`application/octet-stream` was observed) that survives Drive's
auto-detection. The strict mimeType clause silently dropped those
files.

Fix (`drive_client.py`, branch `fix/notes-ingestor-pdf-detection`):

- The `q=` clause now matches `(mimeType = 'application/pdf' or
  name contains '.pdf')`. Name-suffix is the load-bearing check;
  mimeType remains as a redundant signal for non-Samsung uploads.
- The list response `fields=` now includes `mimeType` so the
  diagnostic log line can surface `(name, mimeType)` tuples per
  page — that's what made this bug findable.
- New regression suite at
  `tests/unit/agents/notes_ingestor/test_drive_client.py` asserts
  the OR-clause is present, that a Samsung-style file with the
  bug-bait mimeType yields, and that the diagnostic field set is
  preserved.

After merge: image rebuild + `gcloud run jobs execute
asb-notes-ingestor` once to backfill the missed files (the
watermark only advanced past files that *were* listed, so the
missed files remain visible to the next list call).
