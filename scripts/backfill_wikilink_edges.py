"""One-shot backfill: parse existing notes for [[X]] wikilinks → notes_links.

Per ADR 0053, every new write through ``capture_note`` (and any future
write path that integrates the parser) materializes user-typed
wikilinks as ``notes_links`` rows with ``link_type='wikilink'``. This
script sweeps the existing corpus retroactively.

Run after the schema migration (link_type column added) has been
applied::

    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.backfill_wikilink_edges

Idempotent on (source_note_id, target_note_id, link_type='wikilink')
per the parser's own dedup logic — re-running is safe.

Scope (verified 2026-05-14): the corpus is small + few rows have
``[[X]]`` patterns today. Expected to produce 0-N rows depending on
what's already in the corpus.
"""

from __future__ import annotations

import logging
import os
import sys

from agency_brain.common.wikilink_parser import write_wikilink_edges
from agency_brain.mcp_server.clients import get_config, query_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_wikilink_edges")


def backfill() -> dict[str, int]:
    cfg = get_config()
    rows = query_rows(
        f"SELECT note_id, markdown_content FROM "  # noqa: S608
        f"`{cfg.project_id}.{cfg.outputs_dataset}.{cfg.notes_table}` "
        f"WHERE markdown_content IS NOT NULL "
        f"AND COALESCE(hipaa_isolated, FALSE) = FALSE"
    )
    totals = {
        "notes_scanned": 0,
        "matched": 0,
        "resolved": 0,
        "inserted": 0,
        "skipped_duplicate": 0,
        "skipped_unresolved": 0,
    }
    for r in rows:
        totals["notes_scanned"] += 1
        result = write_wikilink_edges(
            source_note_id=str(r["note_id"]),
            markdown_content=str(r.get("markdown_content") or ""),
            project_id=cfg.project_id,
            dataset_id=cfg.outputs_dataset,
            notes_table=cfg.notes_table,
            links_table=cfg.links_table,
            query_rows=query_rows,
        )
        for key in ("matched", "resolved", "inserted", "skipped_duplicate", "skipped_unresolved"):
            totals[key] += result[key]
        if result["matched"]:
            log.info(
                "backfill.note %s matched=%d inserted=%d skipped_dup=%d skipped_unres=%d",
                r["note_id"],
                result["matched"],
                result["inserted"],
                result["skipped_duplicate"],
                result["skipped_unresolved"],
            )
    return totals


def main() -> int:
    if not os.environ.get("BRAIN_PROJECT_ID"):
        log.error("BRAIN_PROJECT_ID must be set")
        return 2
    totals = backfill()
    log.info(
        "DONE notes_scanned=%d wikilinks_matched=%d resolved=%d inserted=%d "
        "skipped_duplicate=%d skipped_unresolved=%d",
        totals["notes_scanned"],
        totals["matched"],
        totals["resolved"],
        totals["inserted"],
        totals["skipped_duplicate"],
        totals["skipped_unresolved"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
