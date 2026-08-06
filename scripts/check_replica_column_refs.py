#!/usr/bin/env python3
"""PR gate: every ``airtable_replica.*`` column referenced in SQL must exist.

Why this exists
---------------
The F9 audit cleanup (commit 4356c94, 2026-05-28) dropped the ``Created`` and
``Last Modified`` columns from ``airtable/schema.json`` without grepping for
callers. Nine SQL sites across six agents kept querying the now-deleted
``created`` / ``last_modified`` columns and 400'd with ``Unrecognized name``;
``asb-airtable-sync`` aside, the risk-watcher / morning-brief / evening-reflection
/ triage / MCP surfaces failed silently for six days until the runtime alert
fired. This gate makes that class of regression a red PR instead.

What it checks
--------------
For each SQL string under ``src/agency_brain/`` that references
``airtable_replica``, it resolves table aliases and validates every column
reference against the columns that ``schema_mapping.replica_table_schemas``
derives from ``airtable/schema.json`` (source fields + system columns). A
reference to a column that doesn't exist on its table fails the build.

Deliberately conservative — only columns that resolve to a known
``airtable_replica`` table are validated:
  * qualified refs (``t.created``) resolve via the FROM/JOIN alias map;
  * unqualified refs (``created``) are validated only in single-table queries;
  * SELECT-aliases, CTE names, table aliases, and columns on other datasets
    (``agent_outputs`` etc.) are skipped — no schema, no opinion.

SQL that can't be reconstructed/parsed is reported as a skip (not a failure) so
exotic dynamic SQL can't wedge CI; the column check still covers everything
parseable, which is every real query today.

Usage:  python scripts/check_replica_column_refs.py [SRC_ROOT]
Exit 1 on any invalid column reference.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_JSON = REPO_ROOT / "airtable" / "schema.json"
DEFAULT_SRC = REPO_ROOT / "src" / "agency_brain"
REPLICA_DATASET = "airtable_replica"
# Files excluded from the gate, with the reason. Keep this list tiny and
# justified — every entry is a coverage hole. (Currently none.)
EXCLUDED: set[str] = set()
# Other datasets the code queries; collapsed to bare ``db.table`` so they parse
# but are skipped (we only own the airtable_replica schema).
KNOWN_DATASETS = (REPLICA_DATASET, "agent_outputs", "agent_audit_log", "agent_state")

# Collapse a project-qualified backtick table — `{proj}.airtable_replica.tasks`
# or `proj-id.agent_outputs.notes` — down to `db.table` so sqlglot sees a clean
# two-part identifier regardless of the (placeholder) project part.
_TABLE_RE = re.compile(r"`[^`]*?\.(" + "|".join(KNOWN_DATASETS) + r")\.(\w+)`")
_BRACE_RE = re.compile(r"\{[^{}]*\}")  # leftover f-string / .format placeholders
# A string is treated as SQL (not prose/docstring) only if it has SELECT … FROM.
_SQL_SHAPE_RE = re.compile(r"(?is)\bSELECT\b.*\bFROM\b")


def valid_columns_by_table() -> dict[str, set[str]]:
    """{slugified_table_name: {column_name, ...}} from the canonical schema."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from agency_brain.sync import schema_mapping

    schemas = schema_mapping.replica_table_schemas(SCHEMA_JSON)
    return {table: {f.name for f in fields} for table, fields in schemas.items()}


def _render_sql_node(node: ast.AST) -> str | None:
    """Reconstruct a string expression's text.

    Handles literals, f-strings, and ``+`` concatenation. Any sub-expression we
    can't see statically — an f-string ``{...}``, a helper call like
    ``exclude_hipaa('c')``, a variable — renders to the neutral token ``1`` so
    the surrounding SQL still parses (a placeholder value, never a column).
    Returns None for non-string nodes.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else "1"
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "1"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render_sql_node(node.left), _render_sql_node(node.right)
        if left is None and right is None:
            return None
        return (left or "1") + (right or "1")
    return None


def _normalize(sql: str) -> str:
    sql = _TABLE_RE.sub(r"`\1.\2`", sql)  # strip project from db.table
    sql = _BRACE_RE.sub("1", sql)  # value/condition placeholders -> literal
    return sql


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Node ids of module/class/function docstrings — prose, never SQL."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _iter_sql_strings(tree: ast.AST):
    """Yield (lineno, raw_sql) for every SQL-shaped string mentioning the replica.

    Once a node is rendered as SQL, its descendants are consumed so a concat's
    inner fragments aren't re-reported as separate (broken) strings.
    """
    consumed: set[int] = _docstring_ids(tree)
    for node in ast.walk(tree):
        if id(node) in consumed:
            continue
        if isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp):
            text = _render_sql_node(node)
            if text and REPLICA_DATASET in text and _SQL_SHAPE_RE.search(text):
                for desc in ast.walk(node):
                    consumed.add(id(desc))
                yield getattr(node, "lineno", 0), text


def _aliases_to_skip(stmt: exp.Expression) -> set[str]:
    """SELECT-output aliases, CTE names, table aliases — never real columns."""
    skip: set[str] = set()
    for a in stmt.find_all(exp.Alias):
        if a.alias:
            skip.add(a.alias)
    for c in stmt.find_all(exp.CTE):
        if c.alias:
            skip.add(c.alias)
    for t in stmt.find_all(exp.TableAlias):
        if t.name:
            skip.add(t.name)
    return skip


def _source_map(stmt: exp.Expression) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """alias_or_name -> (db, table), plus the list of real (db, table) sources."""
    by_alias: dict[str, tuple[str, str]] = {}
    sources: list[tuple[str, str]] = []
    for t in stmt.find_all(exp.Table):
        db, name = (t.db or ""), t.name
        sources.append((db, name))
        by_alias[t.alias_or_name] = (db, name)
    return by_alias, sources


def check_sql(sql: str, valid: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Return [(table, bad_column)] for replica columns that don't exist."""
    stmt = sqlglot.parse_one(_normalize(sql), dialect="bigquery")
    by_alias, sources = _source_map(stmt)
    replica_sources = [(db, name) for (db, name) in sources if db == REPLICA_DATASET]
    if not replica_sources:
        return []
    skip = _aliases_to_skip(stmt)
    sole_replica = replica_sources[0][1] if len(sources) == 1 else None

    violations: list[tuple[str, str]] = []
    for col in stmt.find_all(exp.Column):
        name = col.name
        if name in skip:
            continue
        qualifier = col.table
        if qualifier:
            resolved = by_alias.get(qualifier)
            if not resolved or resolved[0] != REPLICA_DATASET:
                continue  # column on a non-replica table -> not our schema
            table = resolved[1]
        elif sole_replica is not None:
            table = sole_replica  # unqualified col in a single-table query
        else:
            continue  # unqualified + multi-table -> can't attribute, skip
        cols = valid.get(table)
        if cols is not None and name not in cols:
            violations.append((table, name))
    return violations


def main(argv: list[str]) -> int:
    src_root = Path(argv[1]) if len(argv) > 1 else DEFAULT_SRC
    valid = valid_columns_by_table()

    violations: list[str] = []
    parse_skips: list[str] = []
    for py in sorted(src_root.rglob("*.py")):
        if str(py.relative_to(REPO_ROOT)) in EXCLUDED:
            continue
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError as exc:  # pragma: no cover - source must parse
            print(f"ERROR: could not parse {py}: {exc}", file=sys.stderr)
            return 2
        rel = py.relative_to(REPO_ROOT)
        for lineno, sql in _iter_sql_strings(tree):
            try:
                bad = check_sql(sql, valid)
            except Exception as exc:  # unparseable dynamic SQL -> skip, don't fail
                parse_skips.append(f"{rel}:{lineno} ({type(exc).__name__}: {exc})")
                continue
            for table, col in bad:
                violations.append(
                    f"{rel}:{lineno} — airtable_replica.{table} has no column "
                    f"'{col}' (not in airtable/schema.json + system columns)"
                )

    if parse_skips:
        print("NOTE: SQL strings skipped (unparseable; not validated):", file=sys.stderr)
        for s in parse_skips:
            print(f"  - {s}", file=sys.stderr)

    if violations:
        print("\nREPLICA COLUMN CHECK FAILED — dead/unknown column references:\n")
        for v in violations:
            print(f"  {v}")
        print(
            "\nFix: repoint to an existing column (e.g. the `_airtable_last_modified`\n"
            "system column) or, if the column should exist, add it to\n"
            "airtable/schema.json and rebuild the airtable-sync image. See the\n"
            "F9-cleanup incident (2026-05-28 / 2026-06-03).\n"
        )
        return 1

    print(
        f"OK: all airtable_replica column references in {src_root.relative_to(REPO_ROOT)} "
        "exist in schema.json + system columns."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
