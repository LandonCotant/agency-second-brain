# ADR 0069 — Action memory: commitment extraction (Phase 2)

**Status:** Proposed — 2026-06-13
**Extends:**
- ADR 0037 — single `agent_outputs.*` dataset with a `scope` column (the new `commitments` table lives here, no new dataset)
- ADR 0049 — Gmail-into-corpus (emails land in `agent_outputs.notes` as `note_kind='email'`)
- ADR 0046 / 0031 — calendar + notes ingestion (calendar_event / inbox notes land in the same corpus)
- ADR 0051 — Brain is a signal substrate; capabilities exposed via MCP tools, composition is Claude-side
- ADR 0057 — personal CRM bridge (`pending_followups` covers Airtable `next_followup`; this is the extraction-based complement)

## Context

The Brain has strong **interaction memory** (the corpus: emails, calendar, notes) and partial **action memory** — `pending_followups` surfaces Airtable `next_followup`, but that's a *manually-set* CRM field. Nothing extracts the commitments buried in the corpus: "I'll send the proposal Friday," "Tim will get us the docs next week." These promises — both the ones the operator makes and the ones made to them — are exactly what slips, and today they're invisible unless someone hand-enters a follow-up date.

The 2025–2026 research (deep-research pass, 2026-06-12) names this the highest-value missing layer and the "Absence" capability ("what did I say I'd do that hasn't happened?"). The MEME benchmark (KAIST) showed dependency/absence reasoning is where memory systems fail; commitment tracking is the concrete, scoped slice worth building first. The research also flagged a load-bearing caveat: **extracted structure must augment, not replace, chunk-level retrieval** — a facts-only path loses recall. So commitments sit *alongside* `brain_ask`, never in front of it.

## Decision

### §1 — One post-processor, not four agent changes

All four commitment sources already flow into `agent_outputs.notes`: emails (`note_kind='email'`, ADR 0049), voice memos / quicknotes (`'inbox'`, ADR 0031), calendar events (`'calendar_event'`, ADR 0046), and ad-hoc captures (`'capture'`). Meeting transcripts, when an ingestion path exists, will land there too. So extraction is a **single daily Cloud Run Job** (`asb-commitment-extractor`) reading the corpus — not instrumentation spread across four ingestion agents. One image, one failure domain, one watermark. The corpus is the integration seam (ADR 0051).

### §2 — New table `agent_outputs.commitments`

One row per extracted commitment. The operator (the operator) is the implicit owner; `direction` records which way the promise runs.

| column | type | mode | notes |
|---|---|---|---|
| `commitment_id` | STRING | REQUIRED | UUIDv4 |
| `extracted_at` | TIMESTAMP | REQUIRED | partition column |
| `source_note_id` | STRING | REQUIRED | joins `agent_outputs.notes.note_id` (provenance / evidence link) |
| `source_note_kind` | STRING | REQUIRED | `email` \| `inbox` \| `calendar_event` \| `capture` |
| `direction` | STRING | REQUIRED | `mine` (the operator promised) \| `theirs` (promised to the operator) |
| `counterparty_email` | STRING | NULLABLE | the other party's email (best-effort) |
| `counterparty_name` | STRING | NULLABLE | the other party's name (best-effort) |
| `account_id` | STRING | NULLABLE | Airtable `rec…` id resolved from counterparty; cluster column |
| `commitment_text` | STRING | REQUIRED | what was promised (concise paraphrase) |
| `due_date` | DATE | NULLABLE | explicit or inferred; NULL when none stated |
| `status` | STRING | REQUIRED | `open` \| `done` \| `cancelled`; cluster column |
| `confidence` | FLOAT64 | REQUIRED | [0,1] that this is a real commitment, not noise |
| `reasoning` | STRING | REQUIRED | why the extractor flagged it |
| `agent_run_id` | STRING | REQUIRED | joins `agent_audit_log.events` |

Partition by `extracted_at`, cluster `(account_id, status)`, 730-day TTL (mirrors `triaged_items`, ADR 0024), `deletion_protection = true`. Lives in `agent_outputs` (ADR 0037 — no new dataset).

**Overdue semantics:** a commitment is overdue when `status='open'` AND `COALESCE(due_date, DATE(extracted_at) + COMMITMENT_STALE_DAYS) < CURRENT_DATE()`. Explicit due dates win; dateless promises ("I'll get to it") go stale after `COMMITMENT_STALE_DAYS` (env, default 7) so the dropped-promise case — the whole point — isn't silently lost.

### §3 — Extraction (Gemini 2.5 Flash, structured)

Reuses the CRM Auto-updater's proven pattern (`crm_updater/extractor.py`): `gemini-2.5-flash`, `response_mime_type=application/json` + `response_schema`, `thinking_budget=0`. Per-note the model returns an array of `{direction, counterparty_email, counterparty_name, commitment_text, due_date, confidence, reasoning}`. A confidence floor (`COMMITMENT_MIN_CONFIDENCE`, default 0.6) drops low-signal noise before write. `account_id` resolved by joining `counterparty_email` against `airtable_replica.contacts`/`accounts` (NULL if unresolved). Cost: `gemini-2.5-flash` over ~50 notes/week ≈ single-digit cents/month (well within ADR 0024).

### §4 — Incremental scan + idempotency

New `agent_state.commitment_extractor_watermark` (mirrors `notes_ingestor_watermark`): cursor by `ingested_at`. Each tick scans `notes WHERE ingested_at > @cursor AND hipaa_isolated = FALSE AND note_kind IN @kinds AND note_id NOT IN (SELECT source_note_id FROM commitments)`, then advances the cursor to the max `ingested_at` seen. The watermark avoids re-running Gemini on already-processed (incl. zero-commitment) notes; the `NOT IN commitments` guard makes re-runs idempotent if a note is re-ingested under a new revision. Scheduled ~07:00 PT, after the ingestion jobs (notes 06:00, crm 06:15, calendar 06:30).

### §5 — Surface: the MCP tool (not the paused Morning Brief agent)

`open_commitments(direction=None, account_name=None, days_overdue=0)` MCP read tool (mirrors `open_risk_flags`/`pending_followups`): returns open commitments, overdue-first, split-able by `direction` ("what I owe" vs "what I'm waiting on"). `exclude_hipaa` on both the notes join (source isolation) and the accounts join (HIPAA-account exclusion).

This is the *only* surface v1 adds. The original plan was to add a section to the Morning Brief Cloud Run agent, but its scheduler is **paused** (ADR 0056 — the Local Claude Code routine is the canonical morning surface). That routine already composes from MCP tools, so it picks up overdue commitments simply by calling `open_commitments` — no change to the paused agent, no new image. Wiring the dormant Cloud Run agent would be dead code. If the scheduled Morning Brief is ever revived, an `OverdueCommitmentsReader` is a trivial follow-up.

### §6 — Invariants

- **HIPAA (PRD §4.1):** extractor reads only `hipaa_isolated = FALSE` notes; inherits ingest-time isolation. `exclude_hipaa` on every cross-table read.
- **Drafts-only (PRD §4.7):** writes solely to `agent_outputs.commitments` (its own output table). No Gmail send, no Airtable writes, no source-table writes. `status` starts `open`; marking done is a future tool, not an automated source write.
- **Least-privilege:** new `asb-commitment-extractor-sa` + invoker + custom role `tbCommitmentExtractor` (`bigquery.jobs.create`, `datasets.get`, `aiplatform.endpoints.predict`, `cloudtrace.traces.patch`); dataEditor on `agent_outputs` + `agent_state`; dataViewer on `airtable_replica`. No `agent_audit_log` grant — observability is via Cloud Logging structured logs (the extractor isn't a BaseAgent, so the ADR 0006 emit-on-every-path contract doesn't apply). Passes `sa_allowlist_check` + `least_privilege_check`.
- **Augment, not replace:** commitments are a parallel surface; `brain_ask` retrieval is untouched (research caveat).

## Consequences

- The Brain gains real action memory: "what did I promise and drop?" and "who's behind on what they owed me?" — neither answerable today.
- One new daily Job + one BQ table + one watermark + one MCP tool. The Local Claude Code morning routine surfaces overdue commitments by calling `open_commitments` (no Cloud Run Morning Brief change — its scheduler is paused, ADR 0056).
- `pending_followups` (Airtable, manual) and `open_commitments` (extracted, automatic) coexist — different sources, complementary.
- Prerequisite for Phase 3 (bi-temporal factual memory): establishes the ingestion-time extraction machinery facts will reuse.

## Deferred

- **Marking commitments done** from a conversation (`mark_commitment_status` tool) — v2 once the read path proves useful.
- **Meeting-transcript ingestion** — extractor already handles the `note_kind` the moment such a path lands.
- **Dependency/cascade reasoning** between commitments and facts — Phase 5.
