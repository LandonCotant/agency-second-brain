# ADR 0052 — Synthetic note rows for decisions + wins findability via brain_ask

**Status:** Accepted — 2026-05-14. Extends ADR 0046 (Knowledge Surfacer model + surface) and ADR 0051 (Brain as MCP substrate). Closes the corpus-coverage gap surfaced in the 2026-05-14 morning brief routine smoke.

## Context

`brain_ask` runs BQ `VECTOR_SEARCH` against `agent_outputs.notes` (ADR 0046 §1). The retriever's allowed `note_kind` filter selects `inbox | area | galaxy | calendar_event` (and the implicit `email` / `capture` kinds the ingester produces) — i.e. anything the corpus has explicitly written as a note row.

Decisions and wins live in their own tables (`agent_outputs.decisions`, `agent_outputs.wins`) with their own schemas designed around their semantics (decided_at, choice, prediction, retrospective_30/90/365 dates for decisions; week_of, source_kind, evidence_links for wins). They are **not** in `agent_outputs.notes`, so `brain_ask` can't find them.

This was acceptable when the Brain was a pre-composing system (Evening Reflection wrote Decisions into a Reflection Doc; Brag Spotter wrote Wins into a weekly digest). The 2026-05-14 morning brief routine smoke surfaced the gap: when the routine called `brain_ask("what's pending an action from me / what am I waiting on as of today")`, it got 0 decision rows back. The downstream `brain_ask` consumers in routines need decisions and wins findable through the same semantic interface as notes.

A latent equivalent bug also surfaced: the retriever's hardcoded `INCLUDED_NOTE_KINDS = ('inbox', 'area', 'galaxy', 'calendar_event')` excludes `'capture'` — so the 3 existing `capture_note` rows in prod are invisible to `brain_ask` too, despite the tool docstring promising they'd be retrievable.

## Decision

### §1 — Pattern: synthetic note rows

When a row is written to `agent_outputs.decisions` or `agent_outputs.wins`, **write a companion row to `agent_outputs.notes`** with:

- `note_kind` ∈ {`'decision'`, `'win'`} — new enum values, additive
- `source_drive_file_id = f"decision:{decision_id}"` or `f"win:{win_id}"` — sentinel prefix following the calendar_ingester's `cal:` precedent (ADR 0046 §2)
- `markdown_content` = a synthesis of the canonical row's fields (`# {title}\n\n{body}`)
- `embedding` = `text-embedding-005` over the synthesis, same as `capture_note`
- All other REQUIRED columns populated per the `capture_note` precedent (`extraction_method='synthetic-decision-v1'` / `'synthetic-win-v1'`, `extraction_confidence=1.0`, `page_count=1`, `hipaa_isolated=FALSE`, `revision_id` = the source row's primary timestamp)

The synthetic row is **purely additive** — the canonical `decisions` and `wins` tables stay the source of truth for structured fields. The synthetic note is a retrieval index, not a duplicate of state.

### §2 — Retriever scope widened additively

The retriever's `INCLUDED_NOTE_KINDS` tuple (`agents/knowledge_surfacer/retriever.py`) is widened to include `'capture'`, `'decision'`, `'win'`. The original `'inbox' | 'area' | 'galaxy' | 'calendar_event'` set is preserved — this is an additive list extension, not a replacement.

`'archive'` and `'resource'` stay excluded (cold storage + static templates, per the existing rationale).

### §3 — Update semantics on status change

`mark_decision_status` transitions a decision from `drafted` to `confirmed | dismissed`. The synthetic notes row's `markdown_content` does **not** track the status — `brain_ask` returns the decision row's content regardless of status, and callers can join to the canonical `decisions` table for the status field. This is a deliberate v1 simplification: avoids re-embedding on every status change.

If the status appearing in retrieved content becomes important later, a follow-up can UPDATE the synthetic row's `markdown_content` + re-embed inside `mark_decision_status`.

### §4 — Backfill

A one-shot script (`scripts/backfill_decisions_wins_synthetic_notes.py`) scans existing `decisions` and `wins` rows and writes synthetic notes rows for each. Idempotent on the deterministic synthetic `note_id` (e.g. `f"syn-dec-{decision_id}"`). Current scope: 0 decision rows, 9 win rows in prod as of 2026-05-14.

### §5 — Why this and not embed-and-merge

The alternative considered was adding an `embedding` column to `decisions` and `wins`, then having `brain_ask` UNION across three tables in its SQL. Rejected because:

- Each new retrievable surface adds another UNION arm — `brain_ask` SQL gets messier each time the corpus widens.
- The notes-row pattern reuses the existing VECTOR_SEARCH path verbatim (no SQL change to the retriever beyond the `INCLUDED_NOTE_KINDS` widening).
- The decisions/wins tables stay clean to their semantic purpose; embedding is a retrieval concern that belongs in the retrieval table.
- "Synthetic row in notes" generalizes — the same pattern will cover drafted Tasks (PR 3 follow-up) and any future surface that wants retrievability without a schema migration to its source table.

Trade-off acknowledged: data duplication. Mitigated because decisions/wins are append-mostly (mark_decision_status is the only update path; doesn't touch the synthetic row).

### §6 — Architectural invariant

This is **additive only**. No existing component is replaced:

- `agent_outputs.notes` keeps all existing rows (calendar, email, capture, area, etc.)
- `agent_outputs.decisions` and `agent_outputs.wins` stay the canonical structured store
- The retriever's `_vector_search` SQL is unchanged
- `brain_ask`, `client_summary`, `open_risk_flags` are unchanged
- Librarian's semantic-link writing is unchanged
- `notes_links` table is unchanged

The only schema change is one additional row class in `agent_outputs.notes` (no terraform diff — `note_kind` is `NULLABLE STRING` with no CHECK constraint).

## Consequences

- `brain_ask` queries like "what decisions about X" / "wins this month" / "drafted ideas about Y" now return relevant rows.
- 0 corpus drift today (9 backfill rows, all wins).
- Each new MCP write tool that creates a `decision` / `win` (or, in future, `drafted_task`) automatically gets retrievability for free — the helper function does both writes.

## Follow-ups (out of scope for this ADR)

- **ADR 0053** — explicit wikilink edges + `related_notes` MCP tool. Adds `notes_links.link_type` column, parses `\[\[X\]\]` in `markdown_content` at ingest time, surfaces graph traversal. Same "additive only" principle.
- **Drafted Tasks synthetic notes.** Needs a sync-time hook (post-`airtable_replica.tasks` write); deferred to a later PR.
