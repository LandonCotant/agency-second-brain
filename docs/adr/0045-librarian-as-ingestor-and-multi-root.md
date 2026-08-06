# ADR 0045 — Librarian-as-ingestor + multi-root + cross-Drive realities

**Status:** Accepted
**Date:** 2026-05-08
**Workstream:** Daily-reflection-doc rollout, Phase G (extends ADR 0044)
**Extends (does not supersede):** ADR 0044

## Context

ADR 0044 specified the Librarian as a classify-and-move agent: drop a
file into `Brain/Inbox/Drop/`, the Librarian routes it under
`Brain/Areas/<topic>/`, the Notes Ingestor picks it up on its next
daily tick and writes the corpus row + embedding. The wiki concept
(Areas folder = manually-authored topic dossiers) was the canonical
destination set.

Three things didn't survive contact with the user's actual workflow:

1. **The wiki doesn't live in `Brain/`.** The user's existing client
   work is fully fleshed out in a separate `the agency`
   Shared Drive with `05_CLIENTS/<client>/<sub-template>/` taxonomy
   already in active use (`00_ONBOARDING`, `01_STRATEGY`,
   `06_DELIVERABLES`, `08_MEETING NOTES`, etc.). Asking the user to
   recreate that taxonomy under `Brain/Areas/` would be lossy and
   pointless.

2. **The Notes Ingestor doesn't watch destinations the Librarian uses.**
   When the Librarian moves a file into `Solutions/05_CLIENTS/.../`, the
   Notes Ingestor (configured against `Brain/Inbox/*` env vars) never
   sees it → no corpus row → no embedding → no semantic linking → the
   wiki-as-corpus goal silently fails.

3. **Cross-Shared-Drive moves return 403 in practice.** Even with the
   SA as Content Manager on both Drives, `files.update(addParents=…,
   removeParents=…)` errors with `insufficientFilePermissions` for
   files whose ownership / sharing posture has any edge case (shortcuts,
   files added before the SA was a member, etc.). The "Members can move
   files between Shared Drives" toggle didn't unblock it.

This ADR documents the Phase G adaptations.

## Decisions

### 1. Multi-root destination spec

`LibrarianAreasIndex` accepts a list of `(label, folder_id)` roots and
unions their candidate sets. Each candidate's path is prefixed with its
root's label so the LLM disambiguates across Drives:

  - `brain/personal/wellness` → `Agency Second Brain` Shared Drive / 05_AREAS
  - `clients/06_CLIENT_A/08_MEETING NOTES` → `the agency`
    Shared Drive / 05_CLIENTS

New env var `LIBRARIAN_DEST_ROOTS=label:id,label:id`. Backward-compat
with single-root `BRAIN_AREAS_FOLDER_ID` is preserved.

`LIBRARIAN_EXCLUDED_FOLDER_NAMES` is a comma-separated case-insensitive
folder-name skip list. `02_FINANCE & ACCOUNTING` is excluded by default
in prod — finance content is sensitive and shouldn't be a classification
destination, even though the SA has access via Drive membership.

### 2. Librarian writes to `agent_outputs.notes` directly

A new `librarian/ingestor.py` module handles corpus ingestion. After
classify + move, the Librarian:

  - fetches Drive metadata (revision_id, modifiedTime)
  - dedup-checks `(source_drive_file_id, revision_id)` via `NotesWriter`
    (lifted from notes_ingestor — same dedup contract)
  - embeds via `text-embedding-005` (lifts notes_ingestor's
    `VertexEmbedder`)
  - inserts the `NoteRow` with `note_kind='area'`, `scope` derived from
    the destination root label (`brain` → personal, anything else →
    agency), `hipaa_isolated=False` (Librarian never touches HIPAA per
    ADR 0044 §4)

The Notes Ingestor stays focused on its existing inbox folders (Voice,
Notes, Reading, etc.). Each file is owned by exactly one ingestion
agent; no double-write race.

This is a meaningful architecture shift — the Librarian is now both a
sorter AND an ingestor. The boundary is clean: Notes Ingestor handles
unsorted captures; Librarian handles classified destinations. Together
they cover the full corpus.

### 3. Cross-Shared-Drive copy fallback in the mover

`mover.move()` tries `files.update(addParents/removeParents)` first.
On HTTP 403 specifically, falls back to `files.copy(parents=[dest])`.
The copy lands in the destination Drive with that Drive as owner;
the original stays in the source folder.

The mover returns a `MoveResult` whose `file_id` reflects whichever
file the caller should operate on going forward — the original (when
update succeeded) or the copy (when fallback hit). Downstream agents
(ingestor, linker) work against `move_result.file_id` so the corpus row
points to the destination-Drive-owned file.

The original-still-in-source problem is handled by §4 + §5 below.

### 4. Archive-on-copy

When the move fell back to copy, the original is still in source. The
mover's `archive_processed_original(file_id, source_folder_id)` then
moves the original into `<source>/processed/` — a same-Drive move that
doesn't hit the cross-drive 403 path. The folder is created
idempotently on first need (mirrors the `_uncategorized/` pattern).

`processed/` accumulates originals the user can sweep / auto-trash on
a Drive retention policy when convenient. Active drop area shows only
unprocessed files.

Failure handling: best-effort. Archive failure (race / permission edge
case) doesn't abort the tick — §5's skip-list catches the file on the
next pass.

### 5. Audit-based skip-list

At lister start, query `agent_audit_log.events` for librarian
`file_outcome` rows with `moved=true` over the last 30 days. Build a
`skip_file_ids: set[str]`; the lister filters out file_ids in the set.

Defense-in-depth alongside §4: if the original's archive failed for
some reason, the audit log says we've already processed this file_id
and the next tick skips it. No new copy gets created at the destination.

### 6. Anonymous-filename rename (canonical convention)

When the original filename matches an anonymous pattern
(`Notes_\d{6}_*`, `Untitled*`, `Document (5).*`, voice-memo timestamps,
`IMG_NNNN`, `Screenshot*`, etc.), the destination file is renamed to:

  `YYYY-MM-DD_<topic-slug>_<short-description>.<ext>`

When the user named the file deliberately, the original name is
preserved.

  - **Topic slug** derives from the destination folder path: walk leaf
    → root, skip generic sub-template folders (`08_MEETING NOTES`,
    `01_STRATEGY`, etc.), strip number prefix, slugify. For path
    `clients/06_CLIENT_A/08_MEETING NOTES` →`client-a`.

  - **Description** comes from the classifier's `suggested_description`
    field (NEW in the response_schema; ≤6-word lowercase hyphenated
    descriptor of the file content). Falls back to `note` when absent.

The renamer module (`librarian/renamer.py`) is pure logic; the mover's
`rename_file(file_id, new_name)` does the same-Drive metadata update.

### 7. SA membership widened to span both Shared Drives

The Librarian's runtime SA `asb-librarian-sa` is **Content Manager** on:

  - `Agency Second Brain` Shared Drive (Drop, QuickNotes, 05_AREAS, etc.)
  - `the agency` Shared Drive (read for classification,
    write/move for routing)

Adding the SA to `the agency` widens its blast radius to the
user's full client work. The `02_FINANCE & ACCOUNTING` exclusion at the
classifier level keeps finance content out of the destination set, but
the SA technically has Drive-level access. ADR 0044's HIPAA isolation
still holds — `02_HIPAA_NOTES` in `Agency Second Brain` is excluded at the
lister-role level (`_FORBIDDEN_FOLDER_ROLES = {hipaa}`) and the SA is
not shared on it explicitly.

ADR 0027 §2 invariant (one DWD-grantable SA, two scopes) is preserved.
The Librarian uses non-DWD ADC + folder-share — same pattern ADR 0031
established for the Notes Ingestor's read access, extended to write.

### 8. Default `scope` for client material is `agency`

Phase C's `AreasContextReader` (Reflection's RAG step) was originally
filtering `scope='personal'`. Phase G drops the scope filter entirely
in `areas_context_reader.py` because client material lands as
`scope='agency'` and IS the kind of context the Reflection Doc should
be surfacing (e.g., a Client-A meeting note from last week is exactly
the right RAG hit when today's Reflection mentions Client A).

VECTOR_SEARCH still filters by `note_kind IN ('area','resource')` so
unsorted Inbox-kind notes don't surface.

### 9. VECTOR_SEARCH dimension pre-filter

VECTOR_SEARCH validates embedding dimensions across the entire base
table BEFORE applying inline WHERE filters. With even one pre-ADR-0038
row whose embedding is empty (length 0), the function errors with
"Dimension of column `embedding` does not match." Both the Librarian's
linker and Phase C's areas-context reader pre-filter via a subquery so
VECTOR_SEARCH only sees rows of the expected 768-dim shape:

```
FROM VECTOR_SEARCH(
  (SELECT * FROM `agent_outputs.notes` WHERE ARRAY_LENGTH(embedding) = 768 ...),
  'embedding',
  ...
)
```

Smoke-testing surfaced this bug; not a runtime regression in v1
because Phase C had never had real corpus to query.

## Risks

- **`02_FINANCE` leak via Drive-membership.** SA can read those folders
  as a Drive member even though the classifier won't route there. Code
  paths that call Drive directly (e.g., extractor for files passed in
  via Drop) won't hit this — extractor only sees files the lister
  surfaces, and lister filters by folder. But a future bug that lists
  the whole Solutions tree could surface finance content. Mitigation
  is the explicit folder-name exclusion + classifier never proposing
  it. Worth documenting in code comments where it matters.

- **`processed/` growth.** No automatic retention. User must sweep or
  set Drive retention policy. Mitigation: tiny size relative to the
  corpus; soft cap to consider in future.

- **Ingestor + linker cost.** Each Librarian-routed file = 1 embed
  call + 1 VECTOR_SEARCH + 1-3 INSERTs. ~$0.005 per file per ADR 0038.
  Daily 5am tick processes ≤ 50 files (`LIBRARIAN_MAX_PER_TICK`); cost
  bound is ~$0.25/day worst case. Comfortably under the $50/mo budget.

- **`scope='agency'` everywhere isn't always right.** The user has a
  `personal/` folder under Areas. Files routed there get
  `scope='personal'` correctly because their root_label is `brain`.
  But if the user later renames roots or adds a personal-scope root
  inside Solutions, the convention breaks. Worth a check at deploy
  time when changing root labels.

## References

- ADR 0044 (Drive write via folder share — the architecture this extends)
- ADR 0031 (Notes Ingestor folder-share + ADC pattern)
- ADR 0038 (Embeddings + VECTOR_SEARCH)
- ADR 0027 §2 (DWD allowlist invariant — preserved)
- daily-reflection-doc plan, Phase G (`~/.claude/plans/i-am-wanting-to-zany-stardust.md`)
