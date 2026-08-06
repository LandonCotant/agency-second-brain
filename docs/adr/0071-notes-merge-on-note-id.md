# ADR 0071 — Librarian notes write MERGEs on note_id (no revision append)

**Status:** Accepted — 2026-06-14
**Extends / amends:**
- ADR 0045 — Librarian-as-ingestor (the write path this changes)
- ADR 0054 §2 — Galaxy as drop-to-index folder (the highest-churn caller)
- ADR 0057 — personal CRM bridge (asb-people-sync rewrites the dossier files weekly, the churn source)
- ADR 0025 — streaming-buffer posture (why the path was INSERT-only; why MERGE is nonetheless safe here)
- ADR 0046 — calendar_event already MERGEs `agent_outputs.notes`; this aligns the Librarian kinds with that precedent
- ADR 0068 — retriever hybrid retrieval (the read-side dedup retained as defense-in-depth)

## Context

`agent_outputs.notes` accumulated a new row per sweep for galaxy / people
notes instead of replacing them. Confirmed 2026-06-14: note_id
`FILE_ID_REDACTED_EXAMPLE` ("Client A.md") had **6 rows — one per
weekly sync run.**

Root cause is a two-agent chain, not a single bad writer:

1. **asb-people-sync** (ADR 0057) rewrites the dossier `.md` files in
   `Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/` **in place** every weekly
   run. The file_id is stable, but the AUTO sections (conversation log,
   recent activity, open risks — all enriched from BQ) change week to
   week, so each rewrite bumps Drive's `headRevisionId`.
2. **The Librarian galaxy indexer** (ADR 0054 §2) sweeps Galaxy weekly and
   writes each file to `agent_outputs.notes` through the shared
   `LibrarianIngestor.ingest()`. That path deduped on
   `(source_drive_file_id, revision_id)` and wrote via `insert_rows_json`.
   Because the revision changed, the dedup pre-check **missed** → a new row
   was INSERTed and the prior week's row was **left in place**. `note_id`
   (== stable `file_id`) is constant, so N sweeps produced N rows.

The same latent bug existed for `note_kind='area'` / `'resource'`: they
share `LibrarianIngestor.ingest()`, key `note_id = file_id`, and the Areas
re-index re-sweeps — an edited area/resource file accumulated the same way,
just far less often than the weekly people_sync churn. `calendar_event` was
the only kind already protected (it MERGEs on `(external_id, note_kind)`,
ADR 0046).

The INSERT-only choice came from ADR 0025: DML (MERGE/UPDATE/DELETE) cannot
touch rows still in BigQuery's streaming buffer (~30+ min). But that
constraint does not bind here — the calendar ingester already MERGEs this
same table successfully, and Librarian sweeps are days apart, so the prior
row is always long past the streaming buffer by the time the next sweep
runs.

A read-side dedup (`QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER
BY ... ) = 1`) was added 2026-06-14 in both the Python retriever and the
Cloudflare worker as an immediate band-aid. This ADR fixes the writer so
the band-aid is no longer load-bearing.

## Decision

### §1 — Librarian notes are written via MERGE on `note_id`

`NotesWriter.merge_by_note_id(row)` replaces `write(row)` (INSERT) for the
`LibrarianIngestor` path — i.e. all Librarian-ingested kinds: `galaxy`,
`area`, `resource`. It runs a DML MERGE (via the existing parameterized
query client, mirroring the calendar ingester's JSON-array MERGE) keyed
`ON T.note_id = S.note_id`:

- **MATCHED** → UPDATE the mutable columns in place (revision_id,
  ingested_at, created_at, markdown_content, embedding + embedding_*,
  filename, source_drive_url, note_kind, scope, extraction_*, page_count,
  hipaa_isolated).
- **NOT MATCHED** → INSERT the full row.

`note_id == source_drive_file_id` is globally unique (a file is one note),
so a MERGE on `note_id` alone keeps **exactly one current row per note**.

`triaged_item_id` is deliberately **excluded** from the UPDATE SET — a
Triage back-ref written to a note after ingest must survive a re-sweep.

### §2 — Idempotency keys on `note_id`, not `(file_id, revision_id)`

`LibrarianIngestor.ingest()` pre-checks the latest stored `revision_id` for
the `note_id`:

- **revision unchanged** → return deduped: skip the embed and skip the
  MERGE (preserves the embed-skip cost optimization the old dedup gave).
- **revision changed or note absent** → embed, then `merge_by_note_id` →
  UPDATE-in-place or INSERT.

The content-hash fallback for revision_id (when Drive omits
`headRevisionId`) is unchanged: a SHA-256 of the markdown, so rename /
re-share (which advance modifiedTime but not content) still dedup.

### §3 — Scope: the Notes Ingestor inbox flow is untouched

The change is confined to `LibrarianIngestor` (and therefore the galaxy /
area / resource kinds). The Notes Ingestor inbox flow (`note_kind='inbox'`
voice / quicknotes) keeps its `insert_rows_json` + `(file_id, revision_id)`
dedup — it moves files to a processed folder after one ingest, so it has no
re-sweep and no accumulation. `email` (ADR 0049) and `capture` writers are
likewise untouched.

### §4 — One-time backfill prune

A one-shot DML prunes the duplicate revisions already in the table, keeping
the newest row per `(note_id, note_kind)` for the Librarian-written kinds:

```sql
DELETE FROM `agent_outputs.notes` t
WHERE note_kind IN ('galaxy','area','resource')
  AND ingested_at < (
    SELECT MAX(ingested_at) FROM `agent_outputs.notes` x
    WHERE x.note_id = t.note_id AND x.note_kind = t.note_kind
  );
```

It deletes only older revisions (the newest, possibly-streaming row is
kept), so it is streaming-buffer-safe. Run **after** the writer fix is
deployed so nothing re-creates dupes mid-prune.

### §5 — Retriever dedup retained as defense-in-depth

The `QUALIFY ROW_NUMBER() … PARTITION BY note_id` in
`knowledge_surfacer/retriever.py` and `workers/brain-mcp/src/retriever.ts`
stays. It now guards only against pre-fix historical rows and any future
writer that forgets the contract — it is no longer the primary mechanism.

## Consequences

- One current row per `note_id` for galaxy / area / resource notes; the
  table stops growing by one row per note per sweep.
- Librarian write is now DML, not streaming insert. Safe per the
  sweep-cadence argument above; the calendar ingester is the existing
  precedent on this same table.
- The write contract diverges by source: Librarian = MERGE-on-note_id,
  Notes Ingestor inbox = INSERT + revision dedup, calendar = MERGE on
  external_id. Documented here so the next writer-author picks the right
  one.

## Supersedence

Amends ADR 0045 / ADR 0054 §2: the Librarian notes write is MERGE-on-`note_id`,
replacing the INSERT-append + `(source_drive_file_id, revision_id)` dedup for
the galaxy / area / resource kinds.
