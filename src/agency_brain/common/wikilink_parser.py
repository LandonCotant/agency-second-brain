"""Wikilink (``[[X]]``) parsing + target resolution (ADR 0053).

Used at notes-write time to materialize user-typed graph edges into
``agent_outputs.notes_links``. Kept in ``common`` (not under any one
agent) so multiple write paths can share the parser:

  - ``capture_note`` (MCP) — direct integration in PR 2 of Phase 2 #5
  - ``_insert_synthetic_note`` (decisions/wins) — same call site
  - ``notes_ingestor`` (Cloud Run) — deferred to a follow-up PR
  - Backfill script — sweeps existing notes

Design choices (per ADR 0053 §2):

- Regex match ``\\[\\[([^\\]|]+?)(?:\\|[^\\]]+)?\\]\\]``. Supports
  ``[[Title]]`` and ``[[Title|Display Text]]``; the target is
  ``Title`` (the pipe-alias is just display).
- Resolve by case-insensitive match on ``agent_outputs.notes.filename``.
  Caller picks the BQ client; this module is BQ-shape-agnostic except
  for ``resolve_target``.
- Unresolved targets are skipped (no dangling-link rows in v1).
- Writer is idempotent on ``(source_note_id, target_note_id,
  link_type='wikilink')`` — re-running on the same content is a no-op.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger("agency_brain.common.wikilink_parser")

# Open `[[`, capture target up to `|` or `]`, optionally consume `|<alias>`,
# close `]]`. Non-greedy to handle multiple wikilinks on one line.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")


def extract_wikilinks(markdown: str) -> list[str]:
    """Return the unique ``[[X]]`` targets in ``markdown``, in order of first appearance.

    - ``[[Client A]]`` → ``"Client A"``
    - ``[[Client A|the firm]]`` → ``"Client A"`` (alias dropped)
    - Whitespace around the target is trimmed.
    - Empty matches (``[[]]``) are skipped.
    - Duplicates within the same document collapse to one entry.
    """
    if not markdown:
        return []
    seen: dict[str, None] = {}
    for raw in _WIKILINK_RE.findall(markdown):
        target = raw.strip()
        if not target:
            continue
        seen.setdefault(target, None)
    return list(seen.keys())


class _BQAdapter(Protocol):
    """Minimal BQ surface this module needs — keeps tests easy."""

    def __call__(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


def resolve_target(
    *,
    title: str,
    project_id: str,
    dataset_id: str,
    notes_table: str,
    query_rows: _BQAdapter,
) -> str | None:
    """Case-insensitive lookup of a note by ``filename`` matching ``title``.

    Returns the first matching ``note_id`` or None. HIPAA-isolated rows
    are excluded. A trailing ``.ext`` is stripped from ``filename``
    before comparing: Drive-sourced rows keep their extension
    (``Client A.md``, ``Notes_….pdf``) but wikilinks are written
    without one (``[[Client A]]``). Synthetic notes (calendar events,
    captures, wins) have no extension and pass through unchanged. ADR
    0057 §1 explicitly promised this match.
    """
    rows = query_rows(
        f"SELECT note_id FROM `{project_id}.{dataset_id}.{notes_table}` "  # noqa: S608
        f"WHERE LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) "
        f"      = LOWER(@title) "
        f"AND COALESCE(hipaa_isolated, FALSE) = FALSE "
        f"ORDER BY ingested_at DESC LIMIT 1",
        parameters=[{"name": "title", "type": "STRING", "value": title}],
    )
    return rows[0]["note_id"] if rows else None


def write_wikilink_edges(
    *,
    source_note_id: str,
    markdown_content: str,
    project_id: str,
    dataset_id: str,
    notes_table: str,
    links_table: str,
    query_rows: _BQAdapter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Parse ``markdown_content`` for ``[[X]]`` and INSERT resolved edges.

    Returns ``{"matched": int, "resolved": int, "inserted": int,
    "skipped_duplicate": int, "skipped_unresolved": int}``.

    Best-effort: per-edge failures log and continue. Designed to be
    called from a notes-row writer's tail; never raises on partial
    failure so the parent insert isn't unwound.
    """
    targets = extract_wikilinks(markdown_content)
    result = {
        "matched": len(targets),
        "resolved": 0,
        "inserted": 0,
        "skipped_duplicate": 0,
        "skipped_unresolved": 0,
    }
    if not targets:
        return result

    for target_title in targets:
        try:
            target_note_id = resolve_target(
                title=target_title,
                project_id=project_id,
                dataset_id=dataset_id,
                notes_table=notes_table,
                query_rows=query_rows,
            )
        except Exception:
            log.exception(
                "wikilink_parser.resolve_failed source=%s target=%s",
                source_note_id,
                target_title,
            )
            continue
        if not target_note_id:
            result["skipped_unresolved"] += 1
            continue
        if target_note_id == source_note_id:
            # Self-reference: skip to avoid loop in graph traversal.
            result["skipped_duplicate"] += 1
            continue
        result["resolved"] += 1

        # Idempotency check.
        try:
            existing = query_rows(
                f"SELECT source_note_id FROM "  # noqa: S608
                f"`{project_id}.{dataset_id}.{links_table}` "
                f"WHERE source_note_id = @src AND target_note_id = @tgt "
                f"AND link_type = 'wikilink' LIMIT 1",
                parameters=[
                    {"name": "src", "type": "STRING", "value": source_note_id},
                    {"name": "tgt", "type": "STRING", "value": target_note_id},
                ],
            )
        except Exception:
            log.exception(
                "wikilink_parser.dedup_failed source=%s target=%s",
                source_note_id,
                target_note_id,
            )
            continue
        if existing:
            result["skipped_duplicate"] += 1
            continue

        try:
            query_rows(
                f"INSERT INTO `{project_id}.{dataset_id}.{links_table}` "  # noqa: S608
                f"(source_note_id, target_note_id, similarity, "
                f"computed_at, link_type) "
                f"VALUES (@src, @tgt, 1.0, @now, 'wikilink')",
                parameters=[
                    {"name": "src", "type": "STRING", "value": source_note_id},
                    {"name": "tgt", "type": "STRING", "value": target_note_id},
                    {
                        "name": "now",
                        "type": "TIMESTAMP",
                        "value": now().isoformat(),
                    },
                ],
            )
            result["inserted"] += 1
        except Exception:
            log.exception(
                "wikilink_parser.insert_failed source=%s target=%s",
                source_note_id,
                target_note_id,
            )

    return result
