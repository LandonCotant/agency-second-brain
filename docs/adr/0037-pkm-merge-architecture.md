# ADR 0037 — PKM merge: cloud-native personal knowledge into Agency Second Brain

**Status:** Accepted
**Date:** 2026-05-05
**Workstream:** WS-G (PKM merge — supersedes ADR 0036 §8 deferral of voice-memo lane)

## Context

`Second Brain Ideas/` (uncommitted reference docs at the project root)
describes a local Obsidian + Ollama + LanceDB personal-knowledge-management
system with patterns like `/tonight`, `/reflect`, `/decide`,
`/brag-spotter`, `/seek`, a personal CRM (warmth + last-contact), and an
IPARAG vault taxonomy. Those docs are inspiration; the system the operator will
actually run lives in the existing GCP Agency Second Brain.

Goal: extend the cloud platform so personal life — daily reflections,
decisions, wins, voice memos, reading notes, personal contacts — flows
through the same agent platform that already runs agency operations. One
unified cloud system; no Obsidian; no local stack. Old Obsidian content
re-captured manually.

The merge cleanly **extends** existing scaffolding:

- Notes Ingestor → Triage Pub/Sub → Routing Fan-out is already a
  multi-source composition pipeline (`agents/notes_ingestor/main.py`).
  ADR 0031 §8 reserved "Phase 3 — Notes context loader for triage RAG"
  as a follow-up; embeddings (ADR 0038) unblock that too.
- Morning Brief / Evening Reflection topology (ADR 0029, ADR 0036) is the
  reusable shape for the WS-G4 redesign and for Brag Spotter.
- Risk Watcher's signal-as-data multi-segment loop (ADR 0033/0034) is
  already a parametric tuple list (`agents/risk_watcher/main.py:119`);
  adding a `Personal` segment is a one-tuple change.
- Routing fan-out polls multiple sources via parameter-free SQL builders
  (`routing/polling.py`). Adding a third source for "decisions awaiting
  refinement" is a third builder.

This ADR pins the data-shape and capture-surface decisions for the merge.
The RAG infrastructure decision (embeddings + `VECTOR_SEARCH`) is
co-decided in ADR 0038. The WS-G4 Evening Reflection redesign is a
separate superseding ADR (Phase 1 of the rollout below).

## Decisions

### 1. Single dataset, `scope` column where the distinction matters

All PKM tables live under `agent_outputs.*` alongside agency tables —
**not** a separate `personal_brain.*` dataset. Add
`scope STRING DEFAULT 'agency'` (clustering candidate) only on tables
where the distinction matters at query time:

- `agent_outputs.notes` — `scope` joins `note_kind` and `hipaa_isolated`
  on the cluster key (existing clustering preserved as a column add only;
  see ADR 0038 §3 on why we don't re-cluster).
- `agent_outputs.triaged_items` — `scope` added; existing clustering
  preserved.

PKM-only tables (`reflections`, `decisions`, `wins`, `notes_links`)
**do not** carry the column — their existence implies personal scope.
Goals and Contacts (already mixed agency/personal in the Operations
base — ADR 0020) stay where they are: `agent_outputs.goals` and
`airtable_replica.contacts`. The Personal CRM extension (Phase 4)
adds `Warmth`, `Last Contact`, `Next Followup`, `Relationship Type`
fields directly to the existing Contacts table.

**Why combine over separate dataset:** the categorical line between
agency and personal is genuinely blurry for goals, contacts, and notes
("met a Ross classmate who's a future hire" is both). A separate
dataset would force artificial canonicality decisions on those entities
or accept cross-dataset JOINs anyway. Combining matches the "pragmatic
security over PRD-prescribed defense-in-depth when marginal cost is
real and marginal risk is low" preference (memory:
`feedback_security_vs_cost.md`).

**HIPAA isolation is preserved.** Personal rows always have
`hipaa_isolated = FALSE`, so the existing PR-gate
`scripts/hipaa_filter_check.py` returns empty for them with no
special-casing. The cascade machinery (ADR 0020) is untouched.

### 2. IPARAG adapted to cloud, not copied

The Obsidian-side IPARAG taxonomy (Inbox / Projects / Areas / Resources /
Archives / Galaxy) is adapted, not transplanted:

| Obsidian concept | Cloud mapping |
|---|---|
| **I**nbox | Drive `Brain/Inbox/{Voice,QuickNotes,Reading}` + `Brain/Inbox/HIPAA` (folder convention; reuses ADR 0031 §3 HIPAA aspect) |
| **P**rojects | Already canonical in `airtable_replica.projects` — no Drive folder |
| **A**reas | Drive `Brain/Areas/` (ongoing reference) |
| **R**esources | Drive `Brain/Resources/` (templates, frameworks, static reference) |
| **A**rchives | Drive `Brain/Archives/` (cold storage) |
| **G**alaxy | A `note_kind = 'galaxy'` **column flag** on `agent_outputs.notes` rows — not a folder. Promotion to Galaxy is a column flip, not a file move. |

Rationale for Galaxy-as-flag: in a cloud system the canonical store is
BigQuery, not the file system. A Drive "Galaxy" folder would duplicate
state with the row's canonical content and require move-on-promote
plumbing for marginal value. Promoting an inbox note to a Galaxy
permanent note is just `UPDATE notes SET note_kind='galaxy'`.

`note_kind` enum: `inbox | area | resource | archive | galaxy` —
default `inbox`. Folder role drives the default at ingest; manual
flip post-hoc is supported.

### 3. Capture surfaces: Drive folders + Airtable form

Two surfaces for v1, no Gmail-in:

- **Drive folders** (primary capture for files + voice). Reuses the
  Notes Ingestor pattern from ADR 0031. New folder IDs exposed as env
  vars (`BRAIN_INBOX_VOICE_FOLDER_ID`, etc.); `NOTES_HIPAA_FOLDER_ID`
  is repurposed for `Brain/Inbox/HIPAA/`.
- **Airtable Captures form** (primary capture for fast text). New
  `Captures` Airtable table with `(Note Text, Kind, Scope Hint)`.
  Public form view URL bookmarked on phone. The 15-min Airtable sync
  picks rows up; a small Cloud Run Job materializes them into
  `agent_outputs.notes` and deletes the source Airtable row to keep
  the form clean.

**Gmail-in capture is deferred.** Forwarding personal email to a
`brain@` alias for ingest would require a new `gmail.readonly` DWD
scope (ADR 0027 §2: any new scope requires a superseding ADR). The
drafts-only PRD §4.7 boundary would be re-litigated. The cost is real;
the benefit (faster email-as-capture) is marginal given Drive folder
ingestion already exists. If wanted later, a new ADR with a clean cost
case opens it.

### 4. No new DWD scopes; no new SAs for the foundation

The Notes Ingestor SA (`asb-notes-ingestor-sa` per ADR 0031 §1) already
has Drive folder access via direct sharing (not DWD), BQ write to
`agent_outputs.notes`, and Pub/Sub publish to `asb-triage-input`. The
only new permission needed is **Vertex `aiplatform.endpoints.predict`
for embeddings** — and that's already on `tbNotesIngestor` (per
`notes_ingestor.tf`). No new custom roles, no new SAs, no DWD changes
in Phase 0.

PR-gates `scripts/{drafts_static,least_privilege,hipaa_filter,model_armor}_check.py`
all run unmodified. ADR 0027 audit posture preserved.

### 5. Vertex Gemini multimodal handles audio — no Cloud Speech-to-Text

`gemini-2.5-flash` accepts audio bytes via `Part.from_data(data, mime_type)`
the same way it accepts PDF bytes today (`extractor.py:188-193`). The
existing extractor wires `PDF_MIME_TYPE` as a constant; the refactor
generalizes the Protocol to `(data: bytes, mime_type: str)` and the
production wrapper passes the file's MIME through.

One model handles four input shapes: PDF (existing), Markdown
(passthrough — no LLM call), Google Docs (export to MD), audio
(`audio/mp4`, `audio/mpeg`, `audio/wav` — transcribe to MD with
timestamps). The extraction prompt is keyed by MIME type so PDF, audio,
and doc each get instructions appropriate to their shape.

**Rejected: Cloud Speech-to-Text** as a dedicated transcription
service. Adds a second SA, a second IAM surface, a second cost line,
and another image dependency. Gemini multimodal already does the job
in-process at comparable cost (~$0.001 per voice memo). Document AI
was rejected for the same reasons in ADR 0031 §2; same logic applies.

### 6. Triage publish gated by `note_kind`

Today, every ingested note publishes to `asb-triage-input` (ADR 0031 §3).
Under the merge, only `note_kind='inbox'` notes publish — they're the
actionable captures. `area`, `resource`, `archive`, and `galaxy` notes
are reference / synthesis material, not triage signal. HIPAA notes
still publish with the existing `hipaa_excluded` aspect (the gate
applies to non-HIPAA notes equally — kind-based, not HIPAA-based).

This is the load-bearing reason for combining `scope` into the same
dataset rather than separating: the existing Triage agent's "what work
needs attention" rubric is already general enough to handle personal
inbox captures; forking the Triage pipeline by scope would duplicate
infrastructure for no operational gain.

### 7. Per-folder watermarks already work

`agent_state.notes_ingestor_watermark` is keyed by `folder_id` (ADR 0031
§6); new Brain subfolders just produce new rows. **No state-table
schema change.** Per-folder watermark advancement is per-tick; existing
`MAX_NOTES_PER_TICK` cap (raised to 100 in the closeout addendum)
still bounds backlog drain.

### 8. Drafts-only posture untouched

Every PKM agent that produces output (Evening Reflection v2, Decisions
Reviewer, Brag Spotter, Personal CRM re-engagement signal) drafts to
`owner@example.com` via the existing Gmail draft + Chat
card channels in `routing/channels/`. No new send capabilities; no
auto-write to user-facing surfaces. PRD §4.7 boundary preserved.

The only writes the merge introduces beyond drafts are
**BQ INSERTs to `agent_outputs.{notes,reflections,decisions,wins,notes_links}`
and Airtable writes to `Captures` (delete-after-process) and `Contacts`
(`Last Contact` auto-update via the existing tasks-write PAT)**. None
of these reach external recipients without human approval (Gmail
draft / Chat card → user reads → user actions).

## Phasing

This ADR scopes the architectural decisions; the rollout is five
phases, each one PR ending in a closeout ADR or addendum and a green
PR-checks build:

| Phase | Workstream | ADR | Deliverable |
|---|---|---|---|
| 0a | Foundation | 0037 (this) + 0038 | Schema migrations, Notes Ingestor multi-MIME refactor, Drive layout, Captures form schema. |
| 0b | Captures materializer + embeddings backfill | 0039 | `asb-captures-materializer` Cloud Run Job; backfill via env-var-gated branch in notes-ingestor. |
| 1 | WS-G4 v2 | 0040 (supersedes 0036) | Evening Reflection redesign: two-mode + voice-memo extraction → wins/decisions/follow-ups. |
| 2 | Decisions log | 0041 | `decisions` table consumers (writer surface for `/decide`-equivalent + `asb-decisions-reviewer` Job + routing fan-out third polling source). |
| 3 | Brag Spotter | (addendum to 0041 or new) | Sunday weekly Job; `wins` table aggregation. |
| 4 | Personal CRM | 0042 | Contacts schema additions + Risk Watcher 4th segment (`Personal`) with `PersonalReEngagement` signal. |
| 5 | Connector | (deferred — ADR when corpus warrants) | `notes_links` populated by weekly `VECTOR_SEARCH` job; semantic-link surfacer. |

Phase 5 is post-MVP. The schema is reserved in Phase 0a (`notes_links`
table empty) so it can ship without a follow-up DDL PR. Phase 0b
is split out from 0a so the foundation can land in a reviewable
chunk; ADR 0039 details the materializer + backfill design.

**Note on numbering:** ADR 0036 (Evening Reflection v1) is in flight
and will be superseded by ADR 0040 (NOT 0039). Originally this ADR
projected Phase 1 = 0039, but Phase 0b inserted ADR 0039 between
0a and 1, shifting the planned numbers up by one.

## Alternatives considered

- **Separate `personal_brain.*` dataset.** Rejected. The categorical
  line is genuinely blurry for goals/contacts/notes; a separate
  dataset forces artificial canonicality calls. HIPAA cascade is
  preserved either way (personal rows simply have `hipaa_isolated =
  FALSE`). The ~50 lines of duplicated TTL/audit/cost-tripwire
  Terraform is real but small. The bigger cost is the cognitive
  overhead of "which dataset?" on every new query. Combining wins on
  pragmatism for a 2-person tool.
- **Add Cloud Speech-to-Text as a dedicated transcription service.**
  Rejected (§5). Doubles SA / IAM / cost / image-dep surface for no
  capability gain over Gemini multimodal.
- **Build the local Obsidian system in parallel.** Rejected at the
  Ultraplan stage. Two systems for one person doubles operational
  burden; the cloud platform already has the load-bearing pieces
  (Triage, Routing, Risk Watcher, Morning/Evening rituals).
- **Galaxy as a Drive folder.** Rejected (§2). Would duplicate the
  canonical row content and require move-on-promote plumbing. A
  column flag is one line of SQL.
- **Gmail-in capture for v1.** Deferred (§3). New DWD scope cost
  outweighs marginal capture latency benefit; revisit if the Drive +
  form pair proves insufficient.
- **Manual `[[wikilink]]` syntax for cross-note linking.** Rejected.
  Drive Markdown editors don't render wikilinks; auto-discovered
  semantic links via embeddings (ADR 0038, deferred Phase 5) cover
  the use case better and don't require user-side syntax discipline.

## Consequences

**Positive**

- Notes Ingestor pipeline reused end-to-end. Capture → extract → embed
  → triage gating (kind-based) → BQ + Pub/Sub is one path with three
  new file types (MD, Google Doc, audio) and four new folder roles.
- HIPAA cascade machinery preserved without modification. Personal
  rows have `hipaa_isolated = FALSE`; existing PR-gate filters return
  empty for them.
- Drafts-only PRD §4.7 boundary preserved. No new send capabilities.
- DWD allowlist (ADR 0027) unchanged. No Workspace Admin step. No
  `drafts_boundary_check.py` update.
- ADR 0031 Phase 3 (Notes context loader for triage RAG) unblocked —
  embeddings (ADR 0038) provide the substrate.
- Goals and Contacts stay canonical in their existing tables. No
  cross-system canonicality gymnastics.

**Negative / accepted**

- WS-G4 Evening Reflection (ADR 0036) needs a redesign for Phase 1
  (`/tonight`-style anchor prompt + `/reflect`-style voice-memo
  extraction). Tracked as ADR 0040 (was projected as 0039 before
  Phase 0b inserted; see Phasing note). Refactor scope: ~30% of the
  shipped module (new mode + new readers + new extractor pass; same
  composer + writer dedup pattern).
- Triage Pub/Sub volume goes up by the count of personal inbox
  captures. Today's volume is well below limits (weekly Notes
  cadence + 5-min triage tick); adding ~5–20 personal captures per
  week is negligible.
- `agent_outputs.notes` clustering is **not** changed (BQ doesn't
  allow in-place clustering changes on `deletion_protection=true`
  tables). New `note_kind` and `scope` are added as columns and used
  for filtering at query time, not as cluster keys. ADR 0038 §3
  documents the same constraint.
- One operational point of caution: the Captures form's
  delete-after-process pattern means a sync failure could drop
  capture content if the BQ INSERT succeeds but the Airtable DELETE
  fails. Phase 0 mitigates with a 24h watermark on the sync — only
  delete rows already mirrored to BQ for ≥1 sync cycle.

## References

- ADR 0009 — `agent_outputs` schema design (extending it)
- ADR 0020 — Single Operations Airtable base (Contacts canonical)
- ADR 0024 — Cost guardrails (730d BQ TTL inherits automatically)
- ADR 0027 — DWD delegation surface (preserved untouched)
- ADR 0029 — Morning Brief topology (Phase 1 ADR mirrors)
- ADR 0031 — Notes Ingestor (extended file types, folder taxonomy,
  Phase 3 RAG unblocked)
- ADR 0033 — Risk Watcher topology (Phase 4 extends with Personal segment)
- ADR 0036 — Evening Reflection v1 (Phase 1 redesigns; ADR 0040 supersedes)
- ADR 0039 — PKM Phase 0b (Captures materializer + embeddings backfill)
- ADR 0038 — Embeddings + `VECTOR_SEARCH` (co-decided; RAG substrate)

## Manual operational steps (Phase 0 rollout)

1. Land this ADR + ADR 0038 + Phase 0 schema + Notes Ingestor
   refactor + Captures form schema in one PR.
2. Targeted apply per `feedback_prod_touching_workflow.md`:
   ```
   terraform apply \
     -target=module.agent_runtime.google_bigquery_table.notes \
     -target=module.agent_runtime.google_bigquery_table.triaged_items \
     -target=module.agent_runtime.google_bigquery_table.reflections \
     -target=module.agent_runtime.google_bigquery_table.decisions \
     -target=module.agent_runtime.google_bigquery_table.wins \
     -target=module.agent_runtime.google_bigquery_table.notes_links
   ```
3. Build + push Notes Ingestor image with the multimodal refactor:
   `gcloud builds submit --config=cloudbuild.notes-ingestor.yaml --substitutions=_TAG=adr-0037-pkm-v1 .`
4. **Out-of-band step (user):** Create the Drive folder structure
   per `docs/runbooks/pkm-drive-layout.md` and share with
   `asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com`
   as Editor. Capture the folder IDs into the Cloud Run Job's env vars
   via `terraform/envs/prod/terraform.tfvars`.
5. **Out-of-band step (user):** Create the `Captures` Airtable table
   per `docs/runbooks/pkm-drive-layout.md` §Captures. Bookmark the
   form URL on phone.
6. Smoke: drop one PDF, one MD file, one Google Doc, and one audio
   file in respective Brain folders; run `asb-notes-ingestor`; confirm
   four rows land in `agent_outputs.notes` with `embedding` populated,
   correct `note_kind`, `scope='personal'`. Then submit a Captures
   form entry; confirm round-trip to `agent_outputs.notes` and
   Airtable row deletion within one sync cycle.
