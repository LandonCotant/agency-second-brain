# ADR 0053 — Explicit wikilinks + related_notes traversal

**Status:** Accepted — 2026-05-14. Extends ADR 0045 (Librarian-as-ingestor + semantic neighbor-link writing) and ADR 0052 (synthetic notes for decisions+wins). Closes the "Obsidian-style linking" gap raised 2026-05-14.

## Context

ADR 0045 §5 introduced `agent_outputs.notes_links` for **implicit / semantic** edges — pairs of notes whose embeddings are cosine-near (≥ 0.78 by default), written bidirectionally by the Librarian's Linker during ingestion. Today's table has 4 columns: `source_note_id`, `target_note_id`, `similarity`, `computed_at`. All edges in it are semantic, all from the Librarian.

What's missing: **explicit / wiki edges** — when the user types `[[Client A]]` in a captured note, voice-memo extract, decision body, etc., that's an intentional edge the user is asserting. Today nothing parses that syntax; the `[[X]]` text just sits in `markdown_content` and contributes to embedding similarity at best, but no edge row gets written.

There's also no MCP-side way to walk the graph: callers wanting "what's connected to this note" have to JOIN `notes_links` themselves or ask `brain_ask` indirectly, which is bad at structural questions.

## Decision

### §1 — Add `link_type` NULLABLE column to `notes_links`

Additive schema change. Values:
- `'semantic'` — Librarian's existing edge class. New writes from the Linker stamp this explicitly going forward. Existing rows stay NULL; readers treat NULL as `'semantic'`.
- `'wikilink'` — new in this ADR. User-typed `[[X]]` edges.
- (future) — `'reference'`, `'mention'`, etc. The enum is open at the schema layer; consumers handle unknown kinds gracefully.

No backfill of the existing semantic rows; treating NULL as `'semantic'` at query time is fine. New Linker writes will set the value explicitly.

### §2 — Parse `[[X]]` at note-write time

A new module `src/agency_brain/common/wikilink_parser.py` provides:

- `extract_wikilinks(markdown: str) -> list[str]` — returns the unique targets matched by the regex `\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]`. Supports `[[Title]]` and `[[Title|Display Text]]` (pipe-style aliases — the target is `Title`).
- `resolve_target(title: str) -> str | None` — case-insensitive match against `agent_outputs.notes.filename`; returns `note_id` of the first match or `None`.

A writer helper `_write_wikilink_edges(source_note_id, markdown_content)` runs after a notes-row write (`capture_note` is the first integration point in this PR), parses wikilinks, resolves each target, and INSERTs into `notes_links` with:

```
source_note_id, target_note_id, similarity=1.0,
computed_at=CURRENT_TIMESTAMP(), link_type='wikilink'
```

`similarity=1.0` because explicit edges are user-asserted at max confidence; this lets `notes_links` keep `similarity REQUIRED FLOAT64` without a schema change, and the `link_type` column distinguishes the two edge classes cleanly.

**Unresolved targets** (e.g. `[[Some Future Note]]` where no matching filename exists) are skipped — no "dangling link" rows written. v1 simplification: avoids polluting the table with rows that point at nothing. A future enhancement can write dangling-link metadata.

**Idempotency:** the writer SELECTs against `(source_note_id, target_note_id, link_type)` before INSERT. Re-running on the same content is a no-op.

### §3 — New MCP tool: `related_notes(note_id, depth=1, link_types=None)`

Read tool in `src/agency_brain/mcp_server/tools/read.py`:

- `note_id` — the focal note
- `depth=1` — v1 supports only depth-1 (direct neighbors). Recursive traversal needs cycle detection + cost guards; defer.
- `link_types=None` — filter by edge class. `None` returns both semantic + wikilink edges. Pass `["wikilink"]` or `["semantic"]` to narrow.

Returns:

```python
{
    "links": [
        {
            "target_note_id": str,
            "target_filename": str | None,
            "target_url": str | None,
            "similarity": float,
            "link_type": str,
        }
    ],
    "total": int,
}
```

JOINs `notes_links` → `notes` to enrich the target side with `filename` and `source_drive_url` for caller convenience. `WHERE source_note_id = @note_id`. `ORDER BY similarity DESC, link_type` so the highest-confidence edges land first.

### §4 — Integration points in this PR (PR 2 of 2)

- `capture_note` — calls the wikilink writer after the notes INSERT.
- `_insert_synthetic_note` (added in ADR 0052 / PR 1) — calls the wikilink writer too, so decision/win bodies that include `[[X]]` produce edges.

### §5 — Deferred integrations

- **`notes_ingestor` Cloud Run job** — wikilink parsing at Drive-note ingest time. Requires a Cloud Run image rebuild + rollout (per CLAUDE.md gotcha); deferred to its own PR.
- **Librarian** — could also parse wikilinks during dossier/area classification. Defer until use case proves it.
- **Backfill of existing notes** — `scripts/backfill_wikilink_edges.py` will sweep existing `agent_outputs.notes.markdown_content` for `[[X]]` patterns. Included in this PR.

### §6 — Architectural invariant

Additive only. No existing component is replaced:

- Librarian's semantic-neighbor writer (`linker.py`) is untouched. It will continue to write rows with `link_type` NULL (or `'semantic'` if we re-run it post-merge — out of scope).
- `notes_links` schema gains one NULLABLE column. No row migration.
- `brain_ask` / `client_summary` / `open_risk_flags` / `get_calendar_events` — unchanged.
- `capture_note` keeps its existing write behavior; wikilink edges are an additional side-effect that fails closed (parser/resolver errors log and continue).

## Consequences

- The user can now type `[[Client A]]` in any captured note or decision body and have it materialize as a graph edge.
- `related_notes(some_note_id)` returns the connected set, mixing semantic and explicit edges.
- Sparse today (capture_note isn't heavily used yet); will populate as the corpus grows.

## Follow-ups (separate PRs)

- **notes_ingestor wikilink parsing** — same parser, called at Drive-note ingestion. Image rollout needed.
- **`related_notes` depth > 1** — recursive traversal with cycle detection.
- **Dangling-link tracking** — write `notes_links` rows with `target_note_id` NULL + a separate `target_title` column for `[[X]]` references that don't resolve yet. Enables "unresolved wikilinks" surfacing.
