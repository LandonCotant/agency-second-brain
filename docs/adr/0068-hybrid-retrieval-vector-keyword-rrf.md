# ADR 0068 — Hybrid retrieval for brain_ask (vector + keyword, RRF-fused)

**Status:** Accepted — 2026-06-13
**Extends:**
- ADR 0038 — Embeddings + BQ-native VECTOR_SEARCH (this adds a keyword arm alongside the existing vector arm; it is NOT a managed index and keeps the $0-idle posture)
- ADR 0046 — Knowledge Surfacer retriever (the `Retriever` class this modifies)
- ADR 0051 — Brain is a retrieval substrate; `brain_ask` returns chunks, the host LLM synthesizes
- ADR 0067 — Remote MCP on Cloudflare Workers (retrieval SQL is stateless and ports verbatim; this keeps fusion in SQL so the port stays a copy)

## Context

`brain_ask` is single-channel: embed the query, run BQ `VECTOR_SEARCH` over `agent_outputs.notes`, recency-reweight (`BRAIN_ASK_HALF_LIFE_DAYS=30`, already on). Pure semantic retrieval structurally under-weights sparse-text and exact-token matches — proper nouns, client/person names, acronyms, alphanumerics (Q3, 2026), short calendar/birthday text. This is a daily-use pain: time-bound and entity-lookup queries lean on `get_calendar_events` / `client_summary` today precisely because `brain_ask`'s ranking blurs them.

Independent 2025–2026 research (deep-research pass, 2026-06-12) converges on a hybrid read path — **dense vector + lexical BM25-style + recency, fused with Reciprocal Rank Fusion** — as the best small-corpus design, and finds a **graph channel buys ~2%** at this scale (Mem0g on LoCoMo). So we add the keyword channel + RRF only; the graph channel and structured-fact layers are deferred to later phases.

The competing observation: this is a 2-person tool under a $50/mo budget (ADR 0024). The design has to add zero idle cost and no new managed service, consistent with why ADR 0038 rejected a managed vector index.

## Decision

### §1 — Keyword arm via BigQuery `SEARCH()` + a search index

The keyword channel uses BigQuery's native `SEARCH()` function over `markdown_content` and `filename`. A **search index** (`notes_keyword_idx`) accelerates the per-term predicate. Two facts shape the design:

1. **`SEARCH()` is boolean, not a relevance score — even with an index.** There is no exposed BM25 score in BigQuery. So the keyword arm scores each candidate by **count of matching query terms**. Crucially, **`SEARCH()`'s second argument must be a constant expression** — a per-row value from `UNNEST(@query_terms)` is rejected at plan time (`Argument 2 to SEARCH must be a constant expression`). Since the terms are tokenized in Python at build time, each is bound as its own scalar parameter (`@kw0, @kw1, …`) and the score is `SUM(CAST(SEARCH(markdown_content, @kwN) OR SEARCH(filename, @kwN) AS INT64))`. This gives OR-recall (any term matches) plus a graded rank (more terms = higher), which is what RRF needs. (Caught only by real-BQ execution — unit SQL-shape tests pass either way.)

2. **No native `google_bigquery_search_index` Terraform resource exists** (hashicorp/terraform-provider-google#12388). The index is created via idempotent DDL — `scripts/create_search_index.py` runs `CREATE SEARCH INDEX IF NOT EXISTS … OPTIONS(analyzer='LOG_ANALYZER')` — not a Terraform resource. The index is additive metadata on the `deletion_protection = true` table; it touches no data, schema, clustering, or partitioning, and rolls back with `DROP SEARCH INDEX`. Runbook: `docs/runbooks/search-index.md`.

This is additive to ADR 0038, not a reversal: a keyword search index is not a managed vector index and carries no $30/mo idle node. Index storage is free under BigQuery's per-org limit and negligible at corpus scale; `SEARCH()` scans only the `markdown_content`/`filename` columns of a tens-of-thousands-of-rows table (sub-cent per query, ADR 0024 envelope holds).

### §2 — RRF fusion in a single SQL query

`retrieve()` tokenizes the query (`tokenize_query`: lowercase, split on non-alphanumerics, drop stopwords + 1-char tokens, order-preserving dedup) and runs ONE statement with three stages:

- `vec` CTE — existing `VECTOR_SEARCH` (oversampled to `top_k * OVERSAMPLE_FACTOR`), `ROW_NUMBER() ORDER BY distance ASC` → `vec_rank`.
- `kw` CTE — prefiltered scan scored by matching-term count, `ROW_NUMBER() ORDER BY kw_score DESC` → `kw_rank`, limited to the same candidate pool.
- `fused` CTE — `FULL OUTER JOIN` on `note_id`; `base_rrf = 1/(@rrf_k + vec_rank) + 1/(@rrf_k + kw_rank)` (a missing arm contributes 0); `match_type ∈ {semantic, keyword, both}`.

**Recency is a third RRF channel, not a multiplier.** When `half_life_days > 0`, a `rec_rank` is computed over the fused candidate union (`ROW_NUMBER() ORDER BY created_at DESC`) and the final score is `base_rrf + 1/(@rrf_k + rec_rank)`. This was a correction found by the golden-query eval: RRF scores are compressed near `1/k` (≈0.016–0.033), so the original design — multiplying `base_rrf` by `EXP(-age / half_life)`, which spans orders of magnitude — let recency *dominate* the ranking, flooding the top with recent-but-weak matches and pushing older-but-relevant hits out of the top-k (e.g. an exact "Dermatology" match fell from vector's rank 5 to absent). As an additive RRF channel on the same `1/(k+rank)` scale, recency nudges among already-relevant candidates instead of overwhelming them — sparse-case MRR went 0.67 → 1.00. RRF constant `@rrf_k` defaults to the canonical 60.

Keeping fusion in SQL means the ADR 0067 Worker port stays a near-verbatim copy; only the trivial tokenizer and the per-term score expression (a loop over the term list) are Python/TS logic.

**Known limitation (deferred):** very common tokens (e.g. "event" in a calendar-heavy corpus) add keyword noise that can perturb vague *semantic* queries. The principled fix is IDF term-weighting (down-weight terms that match many docs); deferred as low-ROI — it needs a corpus-stats subquery for marginal gain on non-exact queries, and the keyword arm's job is exact-match recall, which it does decisively.

### §3 — The four load-bearing invariants hold in BOTH arms

`ARRAY_LENGTH(embedding) = 768` · `hipaa_isolated = FALSE` · `note_kind IN UNNEST(@included_kinds)` are present in the prefilter of *both* the vector and keyword arms; `distance_type => 'COSINE'` drives the vector arm. The keyword arm keeps `ARRAY_LENGTH(embedding) = 768` even though lexical hits need no vector — it preserves the HIPAA/kind guards uniformly and excludes the one pre-ADR-0038 length-0 row. Tested in `tests/unit/agents/knowledge_surfacer/test_retriever.py`.

### §4 — Rollout flag + opt-in

`BRAIN_HYBRID_ENABLED` (default `true`) flips `brain_ask` between hybrid and pure-vector with no index teardown. `BRAIN_RRF_K` (default `60`) tunes the fusion constant. The `Retriever` class default is `hybrid=False` so the (retired) Cloud Run Knowledge Surfacer caller and any other consumer keep pure-vector behavior; only MCP `brain_ask` opts in via `cfg.hybrid_enabled`.

### §5 — Resilience: keyword-only degrade

With hybrid on, if the embedding call fails or returns wrong dimensionality, `retrieve()` degrades to a keyword-only query (`_keyword_only_search`) instead of returning `[]` (today's behavior). Exact-match recall survives an embedding/Vertex outage. Keyword-only chunks carry `distance = NULL` → `similarity = 0.0` and `match_type = "keyword"`, so the host LLM knows the hit was lexical, not semantic.

### §6 — Response surface

`brain_ask` chunks gain `match_type` and `rrf_score`. `match_type` tells Claude *why* a chunk surfaced — a `"keyword"` hit with `similarity: 0.0` is an exact-term match, not a bad match, and should not be discounted.

## Validation

A golden-query harness (`scripts/eval/retrieval_eval.py` + `golden_queries.yaml`) runs a fixed real-query set against the live corpus (operator ADC, read-only) under both modes and reports per-query rank + aggregate recall@k / MRR, split into `sparse` (the exact-match cases hybrid must fix) and `semantic` (regression guard). Success = hybrid ≥ vector on every sparse case, no aggregate regression on semantic cases. This is the objective before/after and the standing regression guard for future retrieval edits.

## Consequences

- `brain_ask` returns exact-keyword hits embeddings miss, fused with semantic hits; entity/birthday/acronym lookups stop needing a different tool.
- Zero new managed infra; one additive search index; budget posture unchanged.
- Fusion logic is SQL → ADR 0067 Worker port stays a copy.
- New code paths: `_hybrid_search`, `_keyword_only_search`, `tokenize_query` in the retriever; `hybrid_enabled` / `rrf_k` in MCP `Config`; `match_type` / `rrf_score` on `RetrievedChunk` and the `brain_ask` response.

## Deferred (later phases)

- Graph channel in RRF (low ROI at this scale) — Phase 4/5.
- `gemini-embedding-001` A/B vs `text-embedding-005` — Phase 1.5.
- Commitments / temporal-facts extraction — Phases 2–3.
