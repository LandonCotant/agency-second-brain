#!/usr/bin/env python3
"""Confirm any SQL touching client data carries the canonical HIPAA exclusion clause.

Enforces PRD §4.8 PR security gate and §4.1 layer 2 (sync filter at the source query).

Canonical form a SQL file must satisfy when it reads/writes client-data tables:

    -- HIPAA-EXCLUDE
    ...
    WHERE COALESCE(hipaa_excluded, FALSE) = FALSE
    ...

A SQL file fails the check when, after stripping comments, all of the following hold:
1. It contains a query keyword (`SELECT`, `WITH`, `MERGE`, `UPDATE`, `DELETE`).
2. It references a client-data table — `clients`, `projects`, `tasks`, or any
   `_v` view derived from those (`clients_v`, `client_owners_v`, `projects_v`,
   `tasks_v`, etc.).
3. It is missing EITHER the `-- HIPAA-EXCLUDE` marker (checked in the original
   text, before stripping) OR the canonical `COALESCE(hipaa_excluded, FALSE) = FALSE`
   clause (checked case-insensitively in the stripped text).

Pure DDL files (CREATE/DROP only, no SELECT/WITH/MERGE/UPDATE/DELETE) are exempt —
they aren't reading rows, so a HIPAA WHERE clause has no meaning there.

Files under `tests/fixtures/` are skipped (they intentionally contain violating SQL
to exercise this check).

Usage:
    python scripts/hipaa_filter_check.py [root]   # default root: repo root
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CLIENT_DATA_TABLE_RE = re.compile(
    # ADR 0020: Accounts is the new HIPAA root; clients?/projects?/tasks?
    # remain in the regex so legacy SQL files in fixtures still match.
    r"\b(accounts?|clients?|projects?|tasks?|contacts?|contracts?)(_[a-z][a-z0-9_]*)?\b",
    re.IGNORECASE,
)
QUERY_KEYWORD_RE = re.compile(r"\b(select|with|merge|update|delete)\b", re.IGNORECASE)
CANONICAL_CLAUSE_RE = re.compile(
    # Optional table alias / qualifier before the column name — multi-table
    # JOINs across replica tables often need ``p.hipaa_excluded`` or
    # ``projects.hipaa_excluded`` to disambiguate. The invariant the check
    # enforces is that the cascade filter is applied; whether qualified or
    # not doesn't change that. Surfaced 2026-05-28 when the audit's
    # backfill_triaged_items_account_id.sql added a `p.` qualifier to
    # disambiguate tasks vs projects in the JOIN, breaking the prior
    # unqualified-only regex.
    r"coalesce\s*\(\s*(?:[a-z_][a-z0-9_]*\.)?hipaa_excluded\s*,\s*false\s*\)\s*=\s*false",
    re.IGNORECASE,
)
HIPAA_MARKER = "-- HIPAA-EXCLUDE"

LINE_COMMENT_RE = re.compile(r"--[^\n]*")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

SKIP_PATH_PARTS = ("tests/fixtures",)


def _strip_comments(sql: str) -> str:
    """Remove SQL line and block comments. Used to keep commented-out table
    references from triggering the table-touch check."""
    sql = BLOCK_COMMENT_RE.sub(" ", sql)
    sql = LINE_COMMENT_RE.sub(" ", sql)
    return sql


def _is_skipped_path(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    return any(part in rel for part in SKIP_PATH_PARTS)


def file_violation(sql_path: Path, root: Path) -> str | None:
    raw = sql_path.read_text(errors="replace")
    stripped = _strip_comments(raw)

    if not QUERY_KEYWORD_RE.search(stripped):
        return None  # pure DDL or empty file
    if not CLIENT_DATA_TABLE_RE.search(stripped):
        return None  # doesn't touch client-data tables (after comment strip)

    has_marker = HIPAA_MARKER in raw
    has_clause = bool(CANONICAL_CLAUSE_RE.search(stripped))
    if has_marker and has_clause:
        return None

    rel = sql_path.relative_to(root)
    missing = []
    if not has_marker:
        missing.append(f"{HIPAA_MARKER!r} marker comment")
    if not has_clause:
        missing.append("canonical clause `COALESCE(hipaa_excluded, FALSE) = FALSE`")
    return f"{rel}: touches client data but missing {', '.join(missing)} (PRD §4.1 layer 2)"


def find_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for sql_path in root.rglob("*.sql"):
        if _is_skipped_path(sql_path, root):
            continue
        v = file_violation(sql_path, root)
        if v:
            violations.append(v)
    return violations


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT
    violations = find_violations(root)
    if violations:
        print("HIPAA filter check FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("HIPAA filter check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
