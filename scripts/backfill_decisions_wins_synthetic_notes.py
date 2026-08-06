"""One-shot backfill: synthetic notes rows for existing decisions + wins.

Per ADR 0052, new writes via ``insert_decision`` / ``insert_win`` write a
companion ``agent_outputs.notes`` row alongside the canonical row. This
script catches up existing rows that pre-date that change.

Run once after PR 0052 lands::

    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.backfill_decisions_wins_synthetic_notes

Idempotent on the deterministic synthetic note_id pattern
(``syn-dec-{decision_id}`` / ``syn-win-{win_id}``) — calling twice
re-uses ``_insert_synthetic_note``'s SELECT-for-dedup, so already-
backfilled rows are skipped without re-embedding.

Scope (verified 2026-05-14): 0 decisions, 9 wins. The script will run
cleanly even when both counts are zero.
"""

from __future__ import annotations

import logging
import os
import sys

# Use the same _insert_synthetic_note that the live tools use so the row
# shape is guaranteed identical. Tools live under the mcp_server package
# which is an optional extra — the script requires the same install.
from agency_brain.mcp_server.clients import get_config, query_rows
from agency_brain.mcp_server.tools.write import _insert_synthetic_note

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_decisions_wins_synthetic_notes")


def backfill_decisions() -> tuple[int, int, int]:
    """Returns (total_seen, inserted, skipped)."""
    cfg = get_config()
    rows = query_rows(
        f"SELECT decision_id, title, choice, decided_at "  # noqa: S608
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.decisions` "
        f"ORDER BY decided_at ASC"
    )
    total, inserted, skipped = 0, 0, 0
    for r in rows:
        total += 1
        decision_id = r["decision_id"]
        synthetic_note_id = f"syn-dec-{decision_id}"
        revision_id = r["decided_at"].isoformat() if r.get("decided_at") else decision_id
        ok, err = _insert_synthetic_note(
            note_id=synthetic_note_id,
            note_kind="decision",
            source_record_id=decision_id,
            title=str(r.get("title") or "")[:120],
            body=str(r.get("choice") or ""),
            revision_id=revision_id,
            extraction_method="synthetic-decision-v1-backfill",
        )
        if ok:
            inserted += 1
            log.info("backfill.decision.inserted %s", decision_id)
        elif err:
            log.warning("backfill.decision.failed %s err=%s", decision_id, err)
        else:
            skipped += 1
    return total, inserted, skipped


def backfill_wins() -> tuple[int, int, int]:
    cfg = get_config()
    rows = query_rows(
        f"SELECT win_id, title, summary, captured_at "  # noqa: S608
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.wins` "
        f"ORDER BY captured_at ASC"
    )
    total, inserted, skipped = 0, 0, 0
    for r in rows:
        total += 1
        win_id = r["win_id"]
        synthetic_note_id = f"syn-win-{win_id}"
        revision_id = r["captured_at"].isoformat() if r.get("captured_at") else win_id
        ok, err = _insert_synthetic_note(
            note_id=synthetic_note_id,
            note_kind="win",
            source_record_id=win_id,
            title=str(r.get("title") or "")[:120],
            body=str(r.get("summary") or ""),
            revision_id=revision_id,
            extraction_method="synthetic-win-v1-backfill",
        )
        if ok:
            inserted += 1
            log.info("backfill.win.inserted %s", win_id)
        elif err:
            log.warning("backfill.win.failed %s err=%s", win_id, err)
        else:
            skipped += 1
    return total, inserted, skipped


def main() -> int:
    if not os.environ.get("BRAIN_PROJECT_ID"):
        log.error("BRAIN_PROJECT_ID must be set")
        return 2
    d_total, d_ins, d_skip = backfill_decisions()
    w_total, w_ins, w_skip = backfill_wins()
    log.info(
        "decisions: total=%d inserted=%d skipped=%d / wins: total=%d inserted=%d skipped=%d",
        d_total,
        d_ins,
        d_skip,
        w_total,
        w_ins,
        w_skip,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
