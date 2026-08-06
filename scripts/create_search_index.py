"""Create (or drop / verify) the BM25 search index on agent_outputs.notes.

ADR 0068 — hybrid retrieval gives brain_ask a keyword arm via BigQuery's
``SEARCH()`` function. ``SEARCH()`` works without an index (full column
scan), but a search index makes the per-term predicate fast. There is no
native ``google_bigquery_search_index`` Terraform resource
(hashicorp/terraform-provider-google#12388), so the index is created via
idempotent DDL here rather than in Terraform.

The index is additive metadata on a ``deletion_protection = true`` table —
it does not touch the table's data, schema, clustering, or partitioning,
and is safe to drop at any time (rollback = ``--drop``).

Usage::

    # create (idempotent — CREATE SEARCH INDEX IF NOT EXISTS)
    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.create_search_index

    # verify coverage / status
    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.create_search_index --verify

    # rollback
    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.create_search_index --drop

See docs/runbooks/search-index.md.
"""

from __future__ import annotations

import argparse
import logging

from agency_brain.mcp_server.clients import get_config, query_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("create_search_index")

INDEX_NAME = "notes_keyword_idx"
# LOG_ANALYZER is BigQuery's default — tokenizes on non-alphanumerics,
# lowercases, and is the right fit for free-text markdown prose. Stated
# explicitly so the index definition is self-documenting.
ANALYZER = "LOG_ANALYZER"


def _table_ref(cfg) -> str:
    return f"{cfg.project_id}.{cfg.notes_dataset}.{cfg.notes_table}"


def create() -> None:
    cfg = get_config()
    ref = _table_ref(cfg)
    sql = (
        f"CREATE SEARCH INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON `{ref}`(markdown_content, filename) "
        f"OPTIONS (analyzer = '{ANALYZER}')"
    )
    log.info("creating search index %s on %s", INDEX_NAME, ref)
    log.info("DDL: %s", sql)
    query_rows(sql)
    log.info("done — index builds asynchronously; run --verify for status")


def drop() -> None:
    cfg = get_config()
    ref = _table_ref(cfg)
    sql = f"DROP SEARCH INDEX IF EXISTS {INDEX_NAME} ON `{ref}`"
    log.info("dropping search index %s on %s", INDEX_NAME, ref)
    query_rows(sql)
    log.info("done")


def verify() -> None:
    cfg = get_config()
    sql = (
        f"SELECT index_name, index_status, coverage_percentage, "  # noqa: S608
        f"total_logical_bytes, total_storage_bytes "
        f"FROM `{cfg.project_id}.{cfg.notes_dataset}"
        f".INFORMATION_SCHEMA.SEARCH_INDEXES` "
        f"WHERE table_name = '{cfg.notes_table}'"
    )
    rows = query_rows(sql)
    if not rows:
        log.warning("no search index found on %s", _table_ref(cfg))
        return
    for r in rows:
        log.info(
            "index=%s status=%s coverage=%s%% logical_bytes=%s storage_bytes=%s",
            r.get("index_name"),
            r.get("index_status"),
            r.get("coverage_percentage"),
            r.get("total_logical_bytes"),
            r.get("total_storage_bytes"),
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--drop", action="store_true", help="drop the index (rollback)")
    group.add_argument("--verify", action="store_true", help="report index status")
    args = ap.parse_args()
    if args.drop:
        drop()
    elif args.verify:
        verify()
    else:
        create()


if __name__ == "__main__":
    main()
