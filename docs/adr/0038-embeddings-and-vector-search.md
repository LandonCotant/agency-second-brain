# ADR 0038 — Embeddings + BigQuery `VECTOR_SEARCH` for the notes corpus

**Status:** Accepted
**Date:** 2026-05-05
**Workstream:** WS-G (PKM merge — co-decided with ADR 0037)

## Context

ADR 0037 §1 establishes that PKM data joins agency data in
`agent_outputs.*`. ADR 0031 §8 reserved "Phase 3 — Notes context loader
for triage RAG" as an explicit follow-up. The Obsidian docs in
`Second Brain Ideas/` use a local LanceDB + `nomic-embed-text` vector
store for `/seek`-style semantic recall. All three lines converge on
the same question: how do we provide semantic recall over the notes
corpus in a way that fits this project's cost envelope and operational
posture?

This ADR pins the embedding model, embedding location, and
vector-search infrastructure choice. The Phase 5 Connector
(semantic-link surfacer) and the WS-G4 v2 Reflection composer's
"related to recent notes" anchors both build on what this ADR
establishes.

## Decisions

### 1. Embedding model: `text-embedding-005`, 768 dimensions

`text-embedding-005` is Vertex's general-purpose embedding model on
Gemini-era infrastructure. 768 dimensions matches the
`nomic-embed-text` choice in the Obsidian reference docs (so
dimensionality intuitions transfer) and is the right tier for
personal-scale corpora.

Cost order-of-magnitude: ~$0.0001 per embedding at expected token
counts. Detailed envelope in §6.

**Rejected:**

- `text-embedding-large-exp-03-07` (3072-dim). Higher quality but
  4× storage and 4× search cost for marginal recall gains on a
  personal corpus. The Obsidian docs note 768-dim
  (`nomic-embed-text`) is the right tier until vault size exceeds
  ~2000 notes; the same logic applies here.
- `text-embedding-004` (prior generation). Superseded by 005 with
  zero migration cost; pick the current model.

### 2. Vector storage: BQ-native, NOT a managed Vertex Vector Search index

Embeddings live in `agent_outputs.notes.embedding ARRAY<FLOAT64>`.
Searches use BigQuery's native `VECTOR_SEARCH` table function:

```sql
SELECT base.note_id, base.markdown_content, distance
FROM VECTOR_SEARCH(
  TABLE `agency-brain-demo.agent_outputs.notes`,
  'embedding',
  (SELECT @query_embedding AS embedding),
  top_k => 10,
  distance_type => 'COSINE'
)
WHERE base.scope = 'personal'
  AND base.hipaa_isolated = FALSE
  AND base.note_kind IN ('inbox', 'galaxy', 'area')
```

**Why BQ-native over a managed Vertex Vector Search index:**

- **No idle cost.** Managed Vertex Vector Search's smallest tier is
  roughly $30/month idle on a single index node — that's 60% of the
  project's $50/month budget alert envelope (ADR 0024 / 0030) for
  one capability. BQ `VECTOR_SEARCH` charges per query at standard
  BQ slot rates (~$0.005 per search at our corpus scale).
- **No second store to keep in sync.** Embeddings co-locate with the
  row's other columns. No "did the embedding get re-indexed after
  the row was updated?" failure mode.
- **Existing BQ IAM, audit, partitioning, and TTL apply
  automatically.** No new service to monitor; no new dashboard.
- **Recall ceiling is acceptable at our scale.** BQ
  `VECTOR_SEARCH` runs brute-force KNN for small tables and IVF
  index for larger ones. For a personal corpus (tens of thousands
  of rows max), brute-force is sub-second and gives exact KNN —
  better recall than a managed ANN index, not worse.

The trade-off: managed indexes scale to billion-vector corpora with
lower per-query latency. Neither matters at 2-person scale. Revisit
if `notes` grows past ~10M rows; that's well past v1 horizon.

### 3. Embedding columns are an additive change; clustering unchanged

`agent_outputs.notes` is `deletion_protection = true` and clustered
`(hipaa_isolated, source_drive_file_id)` (per ADR 0009 / 0031).
BigQuery does **not** allow changing clustering keys on a
deletion-protected table in place. Rebuilding to re-cluster would
require export → drop → recreate → reload — which violates the "don't
touch the cascade machinery without thinking through the full chain"
guard (CLAUDE.md Safety rails).

Accepted: clustering stays as-is. New columns are added as plain
columns:

- `embedding ARRAY<FLOAT64>` — the 768-dim vector
- `embedding_model STRING` — pins which model produced it
  (`text-embedding-005` for v1)
- `embedding_generated_at TIMESTAMP` — for audit + re-embed triggers
- `embedding_content_hash STRING` — see §4 idempotency

Filters at query time use `WHERE scope = 'personal' AND
note_kind = 'inbox'` etc. The cluster key still applies to the
load-bearing HIPAA filter, which is what matters operationally.

### 4. Embed on write, content-hash idempotent

The Notes Ingestor extension (Phase 0) embeds every new or modified
`markdown_content` at write time:

1. Compute `content_hash = sha256(markdown_content)` after extraction.
2. SELECT existing row by `(source_drive_file_id, revision_id)`:
   - Hit + matching `embedding_content_hash` → skip embedding
     (idempotent — usually same revision means same content).
   - Hit + mismatched hash → re-embed (rare; defensive).
   - Miss → embed.
3. INSERT with `embedding`,
   `embedding_model = 'text-embedding-005'`,
   `embedding_generated_at = NOW()`,
   `embedding_content_hash = <hash>`.

**Why hash separately when revision_id already keys per-content:**
defensive. The dedup writer keys `(file_id, revision_id)` for the
overall row; the hash adds a second guard against the rare case of
extraction-pipeline drift producing different markdown for the same
revision (e.g., a prompt template version bump). Cost: one more
column, negligible storage.

### 5. Reserve `notes_links` for Phase 5 Connector

Phase 5 (Connector — semantic-link surfacer) will populate
`agent_outputs.notes_links` with
`(source_note_id, target_note_id, similarity, computed_at)`
rows discovered via weekly `VECTOR_SEARCH`. The table is declared in
Phase 0 (empty) so Phase 5 ships without a follow-up DDL PR.

Trigger threshold: ~500 rows in `notes`. Below that, the similarity
space is too sparse to surface useful surprises (everything is a
weak link to everything). Above that, similarity ≥ 0.78 cosine
gives roughly 5–15 high-quality links per new note added — the
shape that makes the surfaced links worth reading.

### 6. Cost envelope

At expected steady-state (rough order of magnitude, 2-person scale):

| Operation | Volume | Unit cost | Monthly |
|---|---|---|---|
| Embed on note ingest | ~50 notes/week | $0.0001 | ~$0.02 |
| `VECTOR_SEARCH` queries | ~300/month (Reflection + Brag Spotter + ad-hoc) | ~$0.005 | ~$1.50 |
| Storage (embeddings) | ~100KB/1000 rows growth | BQ standard | <$0.01 |
| Connector weekly job (Phase 5) | 1 search per new note × ~50/week | $0.005 | ~$1.00 |

Total: well under $3/month. Comfortably below the daily-spend
tripwire (ADR 0030 prod $15/day). Adds <5% to overall project
spend at current scale.

### 7. No new SA, no new role binding

`aiplatform.endpoints.predict` — the permission needed for
`text-embedding-005` via the Vertex prediction API — is **already
granted** on:

- `tbNotesIngestor` (`terraform/modules/agent_runtime/notes_ingestor.tf:96`)
  — for embed-on-write
- `tbAgentTriage` (`terraform/modules/agent_runtime/triage_agent_iam.tf:30`)
  — for future Phase 3 RAG (ADR 0031 §8)
- `tbRiskWatcher` (`terraform/modules/agent_runtime/risk_watcher_iam.tf:47`)
  — for future signal enrichment

The Phase 1/2/3 PKM agents (Evening Reflection v2, Decisions
Reviewer, Brag Spotter) will get their own SAs in their own ADRs,
each with the same `aiplatform.endpoints.predict` line — pattern
copy, no new privilege class.

PR-gates `least_privilege_check.py`, `drafts_static_check.py`,
`hipaa_filter_check.py`, `model_armor_check.py` run unmodified.
ADR 0027 audit posture preserved.

## Alternatives considered

- **Managed Vertex Vector Search index.** Rejected (§2). Idle cost
  alone consumes 60% of the budget alert envelope. The capability
  ceiling (billion-vector ANN) is wasted on a personal corpus.
- **Self-host Qdrant on Cloud Run.** Rejected. Same idle-cost
  problem (Cloud Run min-instance for low-latency vector search
  burns continuously); adds new SA, new IAM, new image, new failure
  mode. The Obsidian docs note Qdrant is appropriate "for desktop
  deployment" — the cloud equivalent on GCP at our scale is
  BQ-native.
- **`text-embedding-large-exp-03-07` (3072-dim).** Rejected (§1).
  Quality gain doesn't justify 4× cost on a personal corpus.
- **Embed on read instead of on write.** Rejected. Per-query
  embedding latency would push `VECTOR_SEARCH` over a second per
  call; embed-on-write amortizes that cost into the ingest pipeline
  where latency doesn't matter. Also wastes embedding API spend on
  re-embeds for repeated queries against the same note.
- **Defer embeddings entirely (Gemini long-context only).** Was the
  default option during planning. Rejected because the user
  explicitly chose `VECTOR_SEARCH` for v1 — and because the
  capability unlocks ADR 0031 §8 Phase 3 (Notes RAG for Triage)
  and the Phase 5 Connector, both of which would otherwise need
  a separate ADR.

## Consequences

**Positive**

- Phase 5 Connector unblocked (schema reserved; populating it is a
  one-Job PR).
- ADR 0031 §8 Phase 3 (Triage RAG) unblocked. A future Triage
  enhancement can read `VECTOR_SEARCH` results from the cached
  context to ground classifications in prior notes.
- WS-G4 v2 Reflection (ADR 0039) can include "related to recent
  notes" anchors via `VECTOR_SEARCH` from the day's calendar /
  triage context.
- Brag Spotter (Phase 3) can find clustered "wins-shaped" content
  across the corpus via similarity search rather than keyword match.
- No new SA, no new IAM, no new managed service, no new image.

**Negative / accepted**

- `agent_outputs.notes` is now wider — `embedding` + 4 sibling
  columns. Storage growth ~100KB per 1000 rows for the embedding
  alone (8 bytes × 768 dims). Still well under the 730d partition
  TTL budget.
- BQ `VECTOR_SEARCH` is GA but newer than other BQ functions the
  project depends on; treat the first cutover as a smoke milestone
  rather than a no-op. Sanity-check query plans on first prod
  query.
- `text-embedding-005` is the current model; future model rotations
  require re-embedding the corpus. The `embedding_model` column
  makes this auditable; a one-shot
  `UPDATE notes SET embedding_model = NULL` triggers a re-embed
  pass through the ingestor's idempotency guard (§4 step 2 misses
  on hash → re-embeds).
- Existing rows in `notes` (from ADR 0031 Phase 1, real Samsung
  Notes captures) will have NULL embeddings until backfilled.
  Phase 0 rollout includes a backfill Job execution that re-runs
  the ingestor in "embed-only" mode against the existing corpus
  (idempotency guard prevents double-write of non-embedding
  columns).

## Backfill procedure (Phase 0 rollout)

1. After schema migration applies, `agent_outputs.notes` rows from
   pre-ADR-0038 ingests have `embedding IS NULL`.
2. Run a one-shot `embedder_backfill.py` (Phase 0 deliverable) that
   SELECTs `note_id, markdown_content` for rows where
   `embedding IS NULL`, embeds them, UPDATEs the row in batches of
   100. (UPDATE is allowed on rows past streaming-buffer window;
   ADR 0025 applies to recent rows only.)
3. Verify post-backfill:
   ```sql
   SELECT
     COUNT(*) AS total,
     COUNTIF(embedding IS NULL) AS missing,
     COUNTIF(embedding_model IS NULL) AS missing_model
   FROM `agency-brain-demo.agent_outputs.notes`
   ```
   Expect `missing = 0` after a single backfill cycle.

## References

- ADR 0009 — `agent_outputs` schema design (extending `notes`)
- ADR 0024 — Cost guardrails (this ADR fits within budget)
- ADR 0025 — Insert-only on streaming-buffer rows (backfill UPDATEs
  apply to rows past the buffer window only)
- ADR 0030 — Daily spend tripwire (alerts apply automatically)
- ADR 0031 — Notes Ingestor (Phase 3 RAG unblocked)
- ADR 0037 — PKM merge architecture (co-decided)
