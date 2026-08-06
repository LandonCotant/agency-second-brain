# ADR 0070 — Bi-temporal factual memory (Phase 3)

**Status:** Proposed — 2026-06-14
**Extends:**
- ADR 0069 — action memory / commitment extractor (this reuses the same post-processor + watermark + entity-resolution machinery)
- ADR 0037 — single `agent_outputs.*` dataset (the new `facts` table lives here, no new dataset)
- ADR 0051 — Brain is a signal substrate; capabilities exposed via MCP tools
- ADR 0025 — insert-only over UPDATE (BQ streaming-buffer DML restriction) — drives the append-only design below
- ADR 0057 — personal CRM bridge (entity resolution against `airtable_replica.{accounts,contacts}`)

## Context

Phases 1–2 gave the Brain hybrid retrieval and action memory. The remaining Sentra-framing layer is **factual memory**: "what's true about an entity, where it came from, and when it changed." Today the only structured entity state is Airtable's *current* fields (warmth, status, account attributes) — there's no history, no provenance, and no capture of facts that live only in unstructured signal ("Acme moved the retainer to $3k", "Tim is now the owner, not the PM"). `client_summary`/`person_summary` can state the present but can't say *when* it changed or *what it was before*.

The 2026 deep-research synthesis (2026-06-12) identified bi-temporal facts as the SOTA design (Zep/Graphiti): two time axes — **event-time** (when a fact is true) and **transaction-time** (when it was recorded) — with **invalidate-don't-delete** on conflict so history and provenance survive. It also flagged the load-bearing caveat reused from Phase 2: extracted facts must **augment, not replace** chunk retrieval.

This is also the highest-*risk* phase: a fact asserted as "true" carries more weight than a commitment surfaced for review. An LLM-hallucinated fact consulted as truth is worse than a missed to-do. The design keeps facts strictly **advisory** — provenance + confidence on every row, a higher confidence floor than commitments, and nothing auto-acts on a fact.

## Decision

### §1 — Scope: entity-attribute facts, append-only

Facts are **attributes of known entities** (accounts + contacts): `(entity, predicate, value)` — e.g. `Acme · retainer · $3k`, `Tim ClientA · role · owner`, `WeCare · status · paused`. Not broad world-facts (noise). Extracted by a daily `asb-fact-extractor` Job — one post-processor over `agent_outputs.notes`, mirroring ADR 0069 §1 (every source already lands in `notes`).

### §2 — Table `agent_outputs.facts` (append-only event log)

| column | type | mode | notes |
|---|---|---|---|
| `fact_id` | STRING | REQUIRED | UUIDv4 |
| `extracted_at` | TIMESTAMP | REQUIRED | **transaction time** (when recorded); partition column |
| `observed_date` | DATE | REQUIRED | **event time** / `valid_from` (when the fact became true; best-effort, defaults to the source note's date) |
| `entity_id` | STRING | NULLABLE | Airtable `rec…` id; NULL if unresolved; cluster column |
| `entity_type` | STRING | NULLABLE | `account` \| `contact` |
| `entity_name` | STRING | REQUIRED | resolved or extracted display name |
| `predicate` | STRING | REQUIRED | normalized snake_case attribute key; cluster column |
| `value` | STRING | REQUIRED | the attribute value |
| `source_note_id` | STRING | REQUIRED | joins `agent_outputs.notes.note_id` — evidence link |
| `source_note_kind` | STRING | REQUIRED | email \| inbox \| calendar_event \| capture |
| `confidence` | FLOAT64 | REQUIRED | [0,1]; floor `FACT_MIN_CONFIDENCE` (default **0.7**, higher than commitments' 0.6) |
| `agent_run_id` | STRING | REQUIRED | traceability |

Partition by `extracted_at`, cluster `(entity_id, predicate)`, 730-day TTL, `deletion_protection = true`. Lives in `agent_outputs` (ADR 0037).

### §3 — Bi-temporal semantics via read-time derivation (no UPDATE)

The user-approved rule is **deterministic same-key supersession**: for a given `(entity_id, predicate)`, the row with the latest `observed_date` (ties broken by `extracted_at`) is the **current** value; earlier rows are **superseded but retained**. Rather than physically `UPDATE`-ing old rows to set `valid_to` — which hits BQ's streaming-buffer DML restriction (ADR 0025) — the table is **append-only** and validity is derived at read time:

- **current** = `ROW_NUMBER() OVER (PARTITION BY entity_id, predicate ORDER BY observed_date DESC, extracted_at DESC) = 1`
- **`valid_to`** = `LEAD(observed_date) OVER (… ORDER BY observed_date)` of the next row for that key (NULL = still current)
- **`superseded_by`** = that next row's `fact_id`

Same bi-temporal model, no DML, no buffer landmines, full history preserved (invalidate-don't-delete is automatic — nothing is ever deleted or mutated). The LLM only extracts `(predicate, value, observed_date, confidence)`; it never judges "contradiction" — supersession is pure deterministic SQL.

**Predicate normalization:** the extraction prompt supplies a canonical key list (`retainer`, `status`, `role`, `renewal_terms`, `primary_contact`, `billing`, …) and asks the model to reuse them; free-form keys are allowed but fragment the same-key rule. Accepted as a known v1 limitation (a future predicate-canonicalizer can merge synonyms).

### §4 — Extraction + incremental scan

Gemini 2.5 Flash structured `response_schema` (`thinking_budget=0`), confidence floor 0.7. `entity_id` resolved from an extracted entity name/email against `airtable_replica.{accounts,contacts}` (NULL if unresolved — fact still recorded by name). `agent_state.fact_extractor_watermark` cursor by `notes.ingested_at` + `NOT IN (SELECT source_note_id FROM facts)` dedup. Scheduled ~07:15 PT (after the commitment extractor). Sources: `email,inbox,calendar_event,capture` (env-tunable; `area`/`galaxy` excluded — galaxy is Airtable-derived, would just re-mint current CRM state).

### §5 — Surface: `entity_facts` MCP tool

`entity_facts(entity_name, include_history=False)` (17th tool) returns **current** facts for the entity (one per predicate), each with `value`, `observed_date` (since when), `confidence`, and `source_url`. `include_history=True` adds superseded rows with their computed `valid_to`. `exclude_hipaa` on both the source-note join (isolation) and the account join (HIPAA-account exclusion). `client_summary`/`person_summary` are **untouched** in v1 (zero regression risk); folding current facts into those briefings is a clean follow-up once the table proves trustworthy.

### §6 — Invariants

- **Advisory only / drafts-only (PRD §4.7):** writes solely to `agent_outputs.facts`; nothing auto-acts on a fact. Every fact carries `confidence` + `source_note_id`. Floor 0.7.
- **HIPAA (PRD §4.1):** reads only `hipaa_isolated = FALSE` notes; `exclude_hipaa` on every cross-table read.
- **Augment, not replace:** facts are a parallel surface; `brain_ask` retrieval untouched (research caveat).
- **Least-privilege:** new `asb-fact-extractor-sa` + invoker + custom role `tbFactExtractor` (`bigquery.jobs.create`, `datasets.get`, `aiplatform.endpoints.predict`, `cloudtrace.traces.patch`); dataEditor on `agent_outputs` + `agent_state`; dataViewer on `airtable_replica`. No `agent_audit_log` grant (Cloud Logging obs). Both SAs allowlisted.

## Consequences

- The Brain gains the bi-temporal factual layer: "what's true about X now, since when, with the source — and what it was before."
- One new daily Job + one BQ table + one watermark + one MCP tool. ~1,000 LOC, mostly reused from ADR 0069.
- Append-only design sidesteps all streaming-buffer DML issues; history is free.
- Completes the three Sentra memory layers (interaction + action + factual).

## Deferred

- **Fold facts into `client_summary`/`person_summary`** (state-aware briefings) — clean follow-up once trusted.
- **Predicate canonicalization** (merge synonym keys).
- **LLM-judged contradiction** supersession (richer than same-key; only if the deterministic rule proves too coarse).
- **Cross-fact dependency / cascade** ("decision X rested on fact Y, now stale") — the original Phase 5; still deferred as low-ROI at this scale.
