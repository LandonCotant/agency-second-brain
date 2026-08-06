# ADR 0054 — Librarian: Resources bucket + Galaxy as capture surface

**Status:** Accepted — 2026-05-16. Extends ADR 0037 (PKM merge / IPARAG vision) and ADR 0045 (Librarian-as-ingestor + multi-root). Supersedes ADR 0037 §2's sub-decision that rejected a Galaxy folder. §1 (Resources) shipped 2026-05-16; §2 (Galaxy indexing) shipped 2026-05-18.

## Context

ADR 0037 declared IPARAG (Inbox / Projects / Areas / Resources / Archives / Galaxy) as the target Drive layout. The shipped Librarian (ADR 0044 + ADR 0045) only delivers the **I → Areas leg**: sweep `Brain/Inbox/05_DROP/` + aged `06_QUICKNOTES/`, classify via Gemini against multi-root destinations, move + write the corpus row + write semantic neighbor edges. The remaining IPARAG transitions named in ADR 0037 §2 were never built.

Two real-world adjustments to the as-designed IPARAG:

- **No Projects folder.** Airtable's `Projects` table is canonical and Solutions' `05_CLIENTS/` is where client deliverables live. A Brain `Projects/` folder would be a third home for the same concept and create routing ambiguity for the classifier.
- **Keep Brain Areas.** Solutions covers client work, but Areas owns personal-domain content Solutions doesn't (Strategy doc, Briefs, Reflections, Weekly Reviews, MBAn, wellness, personal CRM). Routine outputs (`update_weekly_doc`) already write there.

ADR 0037 §2 explicitly rejected a Galaxy *folder* on the grounds that it "would duplicate canonical row content and require move-on-promote plumbing." That argument applies only to the move-on-promote model. The model adopted here is **drop-to-index**: the file IS the source of truth; the Librarian indexes its presence with `note_kind='galaxy'` without moving anything. ADR 0037 §2's Galaxy-as-flag invariant stays intact — Galaxy is now both a folder AND a flag (folder is the capture surface; flag is the canonical kind).

Archives is real but deferred — the initial design assumed `airtable_replica.projects.drive_folder_url` exists; it does not (only `Projects.Scope Document` URL is there; per-client Drive folders live on `Accounts.Google Drive Folder`). Archiving a whole client folder when one project completes is wrong (multi-project clients). The Archives PR is deferred until Resources + Galaxy land + run a week AND the archive-trigger model is re-validated against Airtable shape.

## Decision

### §1 — Resources bucket on `AreaFolder`

`AreaFolder` gains a `bucket: str = "areas"` field. Allowed values: `"areas"` (default — also covers `clients` roots) and `"resources"`. The field flows through the index → classifier → ingestor pipeline. The classifier's response schema is **unchanged** — the LLM still picks one of the candidate paths verbatim; bucket is carried out-of-band on the matched `AreaFolder` so there's no LLM-side schema churn.

`LIBRARIAN_DEST_ROOTS` env-var grammar widens additively:

```
# 2-segment legacy form — bucket defaults to "areas":
LIBRARIAN_DEST_ROOTS="brain:<areas_id>,clients:<clients_id>"

# 3-segment form (this ADR) — explicit bucket:
LIBRARIAN_DEST_ROOTS="brain:<areas_id>,resources=resources:<resources_id>,clients:<clients_id>"
```

`parse_roots_env` returns `(bucket, label, folder_id)` triples for both forms. `LibrarianAreasIndex.__init__(roots=...)` accepts both 2-tuples (auto-promoted to `bucket="areas"`) and 3-tuples.

The classifier prompt teaches the Areas-vs-Resources distinction with one paragraph:

> An Area is an ongoing responsibility OR the work itself (a meeting notes file, a client strategy doc, a personal Reflection, a per-area log). A Resource is a template, framework, factual reference, or prompt — something you reach for *when doing work*, reusable across projects. When in doubt, prefer Areas; Resources is reserved for content obviously reusable across multiple projects/clients.

`LibrarianIngestor` maps bucket → kind/scope:

| `AreaFolder.bucket` | `note_kind` | `scope` (default) |
|---|---|---|
| `"areas"` (incl. `clients`) | `area` | `agency` if root_label ∉ {`brain`, `personal`, `resources`} else `personal` |
| `"resources"` | `resource` | `personal` (Resources lives in Brain) |

The Knowledge Surfacer's `INCLUDED_NOTE_KINDS` widens additively to include `'resource'`. Templates and references are useful grounding for `brain_ask` — "what discovery-call template do I have?" should return content from the Resources bucket. Excluding `'resource'` was a v0 assumption from ADR 0046, not a hard constraint; this ADR flips it. `'archive'` stays excluded (archived ≠ retrievable as a fresh signal).

### §2 — Galaxy as capture surface (drop-to-index)

A new top-level Brain folder `Brain/05_GALAXY/` becomes the user-facing capture surface for promoted permanent / atomic notes. The Librarian gains a `GalaxyIndexer` sweep pass (`src/agency_brain/agents/librarian/galaxy_indexer.py`) that runs as part of the same daily tick, after the Drop/QuickNotes loop:

1. Recursively lists children of `BRAIN_GALAXY_FOLDER_ID` (depth ≤ 3, same cap as `LibrarianAreasIndex`).
2. For each file: builds a `DropFile` with `parent_folder_role="galaxy"`, extracts markdown (markdown / text fast-path; Google Doc → export-as-markdown; everything else routed through `GeminiMultimodalExtractor.extract`).
3. Calls `LibrarianIngestor.ingest(...)` with `kind_override=NoteKind.GALAXY`, `scope_override=Scope.PERSONAL`, `dest_folder=None` — **the file does not move.** The path inside Galaxy is the user's own organization choice. Dedup is the same `(source_drive_file_id, revision_id)` pre-INSERT SELECT as the Drop flow.
4. On a fresh write, runs `LibrarianLinker.link_for_drive_file(drive_file_id, dossier_doc_id=None)` to write `notes_links` semantic neighbors (top_k=3, cosine ≥ 0.78; same defaults as the existing linker). On a dedup hit, the linker is skipped — the existing row already has its neighbors from the prior pass.
5. Emits a per-file audit row via `LibrarianAuditWriter.emit_file_outcome` with `event_kind="galaxy_index"` (the writer routes on `outcome.from_folder_role`). Run summary picks up `galaxy_listed / galaxy_indexed / galaxy_deduped / galaxy_failed` counters on `LibrarianSummary`.

The ingestor's signature gains two optional parameters — `kind_override: NoteKind | None` and `scope_override: Scope | None` — so non-classifier callers can pin the row's kind/scope independent of bucket inference. Drop-flow callers continue to pass neither, preserving the bucket→kind mapping introduced in §1.

This **supersedes ADR 0037 §2's sub-decision** that rejected a Galaxy folder. The §2 rejection cited move-on-promote plumbing as the disqualifier; drop-to-index avoids that entirely. The Galaxy-as-flag invariant from ADR 0037 §2 stays intact — `note_kind='galaxy'` is still the canonical signal that gates Galaxy behavior downstream, and `'galaxy'` was already in the Knowledge Surfacer's `INCLUDED_NOTE_KINDS` (no retriever change needed for §2).

`BRAIN_GALAXY_FOLDER_ID` is the env-gate; unset disables the pass silently (back-compat for tenants that don't want Galaxy). The Cloud Run Job exit code surfaces Galaxy failures the same way it does Drop failures — non-zero if any per-file failure was recorded.

### §3 — Deferrals (explicit non-decisions)

- **Projects classification.** Airtable + Solutions canonical. Not a Librarian destination.
- **Areas Monitoring** (Phase 4-style — proactive Brain "you should look at this Area" pings). Out of scope.
- **Archives lifecycle.** Real need but design gap: `airtable_replica.projects` doesn't carry a per-project Drive folder URL today (only `Projects.Scope Document` = signed SOW). Three open options when revisiting:
  - **Option A** — add `Drive Folder URL` to the Airtable `Projects` table; archive on project status flip.
  - **Option B** — derive per-project folder convention from Solutions Drive layout (`05_CLIENTS/<client>/<project>/`) and walk Drive directly. Brittle vs. inconsistent client conventions.
  - **Option C** — age-only fallback for personal `Brain/02_AREAS/<per-area>/` subfolders, no Airtable trigger. Simpler but doesn't archive client content.

  Recommend re-validating after Resources + Galaxy run for a week. Folder `Brain/04_ARCHIVES/` may be created up front as an inert placeholder.

## Consequences

- New `bucket` field on `AreaFolder` is backward-compatible: default `"areas"` keeps every existing call site working. Legacy 2-tuple `roots` and 2-segment env entries still parse.
- Retriever widening is purely additive — existing `brain_ask` behavior unchanged for non-Resources content; Resources rows previously written via the Solutions sweep (`note_kind=resource` from `RESOURCES` folder role in `notes_ingestor.models`) become newly retrievable.
- LLM-side schema unchanged — no classifier prompt break or response_schema migration.
- Galaxy pass (§2) is opt-in via env var; absent env keeps current behavior.
- ADR 0037 §2 partially superseded: the Galaxy-folder rejection is overridden; the Galaxy-as-flag invariant and the IPARAG layout are preserved + extended.

## Implementation note

§1 landed in PR #152 (`feat/librarian-resources-bucket`). §2 landed in the follow-up PR (`feat/librarian-galaxy-indexer`). Both share this ADR rather than splitting it across two files because the decisions are tightly coupled (both are IPARAG completions on top of the existing Librarian) and the load-bearing rationale (drop-to-index vs move-on-promote, bucket-as-out-of-band-metadata) reads cleanly as one unit.
