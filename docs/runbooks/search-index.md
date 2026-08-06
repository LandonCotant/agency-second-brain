# Runbook — BM25 search index for hybrid brain_ask (ADR 0068)

The keyword arm of `brain_ask`'s hybrid retrieval uses BigQuery's `SEARCH()`
over `agent_outputs.notes(markdown_content, filename)`. A search index makes
that predicate fast. There is no native Terraform resource for BigQuery
search indexes (provider issue #12388), so the index is managed via DDL
through `scripts/create_search_index.py`.

The index is **additive metadata** on a `deletion_protection = true` table —
it does not touch table data, schema, clustering, or partitioning, and is
safe to create or drop at any time.

## Create (idempotent)

```bash
BRAIN_PROJECT_ID=agency-brain-demo \
  .venv/bin/python -m scripts.create_search_index
```

Runs `CREATE SEARCH INDEX IF NOT EXISTS notes_keyword_idx ON
\`…agent_outputs.notes\`(markdown_content, filename) OPTIONS(analyzer='LOG_ANALYZER')`.
The index builds **asynchronously** — the command returns immediately; the
index is populated in the background by BigQuery (free background indexing).

## Verify

```bash
BRAIN_PROJECT_ID=agency-brain-demo \
  .venv/bin/python -m scripts.create_search_index --verify
```

Reports `index_status` and `coverage_percentage` from
`agent_outputs.INFORMATION_SCHEMA.SEARCH_INDEXES`.

**Expected at current scale: `index_status = TEMPORARILY DISABLED`,
`coverage = 0%`.** BigQuery does not *build* a search index until the table
crosses its size threshold (~10 GB); the `notes` table (hundreds of rows,
a few MB) is far below it. This is fine and expected — **`SEARCH()` still
works without an active index** via full column scan, which is sub-cent at
this scale, so hybrid retrieval is fully functional. The index definition is
in place and will auto-activate (status → `ACTIVE`, coverage → 100%) if the
corpus ever grows past the threshold. No action needed.

## Rollback

```bash
BRAIN_PROJECT_ID=agency-brain-demo \
  .venv/bin/python -m scripts.create_search_index --drop
```

To disable the keyword arm *without* dropping the index, set
`BRAIN_HYBRID_ENABLED=false` on the MCP server (instant revert to
pure-vector; no index teardown).

## Cost

- **Index storage:** free under BigQuery's per-org search-index storage
  limit (100 GB); the notes index is a few MB at corpus scale.
- **Background indexing:** free.
- **Query:** `SEARCH()` scans only `markdown_content`/`filename`; sub-cent
  per `brain_ask` at current volume. Well within the ADR 0024 $50/mo
  envelope.

## When to rerun

Never on a schedule — BigQuery maintains the index incrementally as
`notes` rows are inserted. Only rerun `--create` if the index was dropped,
or `--verify` when debugging keyword-arm coverage. If you change the indexed
columns or analyzer, `--drop` then `--create`.
