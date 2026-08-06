#!/usr/bin/env python3
"""Enforce Python ↔ Terraform alignment for the Airtable→BigQuery type map
and the sync-layer system columns.

PR #173 (2026-05-28 production-readiness audit) shipped three new Airtable
type entries (``richText``, ``dateTime``, ``autoNumber``) to
``src/agency_brain/sync/schema_mapping.py`` but missed the parallel
``airtable_to_bq_type`` map in
``terraform/modules/data_pipeline/replica_tables.tf``. CI caught the
divergence at the ``terraform-fmt-validate`` step because the for-expression
in ``replica_tables.tf:96`` tried to look up an unknown key. That's the
seventh time in three weeks that an out-of-sync schema edit broke prod or
CI; the docstring in ``schema_mapping.py`` even warns about this exact
failure mode. The audit's first hardening workstream (W1) is this script.

What it does:
- Loads ``_TYPE_MAP`` and ``_SYSTEM_COLUMNS`` from the Python file using
  ``ast.literal_eval`` (no project install needed; runs on bare python:3.12-slim).
- Parses ``airtable_to_bq_type`` and ``system_columns`` from the Terraform
  file with regex scoped to those specific local blocks.
- Asserts:
  1. Identical keys + ``(type, mode)`` tuples across the two type maps.
  2. Identical ordered list of ``(name, type, mode, description)`` across
     the two system-column declarations. Order matters because the BQ
     column order matches the iteration order; reordering one side without
     the other would silently corrupt downstream queries that name columns
     by position.

Failure mode is a human-readable diff printed to stdout and exit code 1.
Parse failures exit 2 (so the operator can distinguish "drift detected"
from "script broke").

Usage:
    python scripts/check_schema_mapping_alignment.py

Optional positional argument: repo root (default: parent of this script).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

PY_SCHEMA_MAPPING = "src/agency_brain/sync/schema_mapping.py"
TF_REPLICA_TABLES = "terraform/modules/data_pipeline/replica_tables.tf"


# ---------------------------------------------------------------------------
# Python side: extract literals via AST so the script runs without installing
# the project package.
# ---------------------------------------------------------------------------


def _extract_python_assignment(source: str, target_name: str) -> Any:
    """Return the literal value of a top-level assignment ``target_name = <expr>``.

    Walks ``ast.Module.body`` looking for an annotated or plain assignment
    whose target is ``target_name``, then ``ast.literal_eval``s the value
    expression. Implicit string concatenation inside dict literals (used by
    ``_SYSTEM_COLUMNS[5]["description"]``) is folded by the tokenizer before
    ``literal_eval`` sees it, so the script gets the joined string.
    """
    tree = ast.parse(source)
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        if value is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == target_name:
                return ast.literal_eval(value)
    raise KeyError(f"top-level assignment {target_name!r} not found")


def python_type_map(source: str) -> dict[str, tuple[str, str]]:
    raw = _extract_python_assignment(source, "_TYPE_MAP")
    if not isinstance(raw, dict):
        raise TypeError(f"_TYPE_MAP must be a dict, got {type(raw).__name__}")
    out: dict[str, tuple[str, str]] = {}
    for k, v in raw.items():
        if not isinstance(v, tuple) or len(v) != 2:
            raise TypeError(f"_TYPE_MAP[{k!r}] must be a (type, mode) tuple")
        out[str(k)] = (str(v[0]), str(v[1]))
    return out


def python_system_columns(source: str) -> list[dict[str, str]]:
    raw = _extract_python_assignment(source, "_SYSTEM_COLUMNS")
    if not isinstance(raw, tuple):
        raise TypeError(f"_SYSTEM_COLUMNS must be a tuple, got {type(raw).__name__}")
    out: list[dict[str, str]] = []
    for i, col in enumerate(raw):
        if not isinstance(col, dict):
            raise TypeError(f"_SYSTEM_COLUMNS[{i}] must be a dict")
        out.append({k: str(v) for k, v in col.items()})
    return out


# ---------------------------------------------------------------------------
# Terraform side: regex-parse the two specific local blocks.
# ---------------------------------------------------------------------------


_TF_TYPE_ENTRY_RE = re.compile(
    # Each line in airtable_to_bq_type looks like:
    #   <key>                = { type = "<bq>", mode = "<mode>" }
    # Keys are bare identifiers (no quotes), values are double-quoted strings.
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*"
    r'type\s*=\s*"([^"]+)"\s*,\s*'
    r'mode\s*=\s*"([^"]+)"\s*'
    r"\}\s*$",
    re.MULTILINE,
)


def _extract_tf_block(source: str, local_name: str, open_char: str, close_char: str) -> str:
    """Return the substring inside ``local_name = <open>...<close>``.

    Tracks bracket depth so nested ``{}`` (the type-map value side) or nested
    ``{}`` inside list items (the system-columns elements) don't terminate
    the outer block prematurely.
    """
    pattern = rf"\b{re.escape(local_name)}\s*=\s*{re.escape(open_char)}"
    needle = re.search(pattern, source)
    if needle is None:
        raise KeyError(f"local {local_name!r} not found in terraform source")
    start = needle.end()
    depth = 1
    i = start
    while i < len(source):
        ch = source[i]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return source[start:i]
        i += 1
    raise ValueError(f"unbalanced {open_char}{close_char} block for {local_name!r}")


def terraform_type_map(source: str) -> dict[str, tuple[str, str]]:
    block = _extract_tf_block(source, "airtable_to_bq_type", "{", "}")
    out: dict[str, tuple[str, str]] = {}
    for match in _TF_TYPE_ENTRY_RE.finditer(block):
        key, bq_type, mode = match.group(1), match.group(2), match.group(3)
        out[key] = (bq_type, mode)
    return out


_TF_SYSCOL_KV_RE = re.compile(
    r'^\s*(name|type|mode|description)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$',
    re.MULTILINE,
)


def terraform_system_columns(source: str) -> list[dict[str, str]]:
    """Parse the ``system_columns`` list-of-objects from the HCL source.

    Walks the block char-by-char tracking brace depth to split on the
    top-level commas between objects; within each object, regex-extracts
    the four required ``key = "value"`` lines.
    """
    block = _extract_tf_block(source, "system_columns", "[", "]")
    objects: list[str] = []
    depth = 0
    current_start = -1
    for i, ch in enumerate(block):
        if ch == "{":
            if depth == 0:
                current_start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and current_start != -1:
                objects.append(block[current_start:i])
                current_start = -1
    out: list[dict[str, str]] = []
    for obj_src in objects:
        kvs: dict[str, str] = {}
        for match in _TF_SYSCOL_KV_RE.finditer(obj_src):
            key, value = match.group(1), match.group(2)
            # HCL string escapes: only \" and \\ are common; preserve raw
            # content otherwise so descriptions with em-dashes/§ pass through.
            value = value.replace('\\"', '"').replace("\\\\", "\\")
            kvs[key] = value
        out.append(kvs)
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_type_maps(
    py: dict[str, tuple[str, str]],
    tf: dict[str, tuple[str, str]],
) -> list[str]:
    findings: list[str] = []
    only_py = sorted(set(py) - set(tf))
    only_tf = sorted(set(tf) - set(py))
    for k in only_py:
        findings.append(f"type_map: '{k}' declared in Python ({py[k]}) but missing from Terraform")
    for k in only_tf:
        findings.append(f"type_map: '{k}' declared in Terraform ({tf[k]}) but missing from Python")
    for k in sorted(set(py) & set(tf)):
        if py[k] != tf[k]:
            findings.append(f"type_map: '{k}' diverges — Python={py[k]} Terraform={tf[k]}")
    return findings


def diff_system_columns(
    py: list[dict[str, str]],
    tf: list[dict[str, str]],
) -> list[str]:
    findings: list[str] = []
    if len(py) != len(tf):
        findings.append(f"system_columns: length mismatch — Python={len(py)} Terraform={len(tf)}")
    required_keys = ("name", "type", "mode", "description")
    for i, (py_col, tf_col) in enumerate(zip(py, tf, strict=False)):
        for key in required_keys:
            py_val = py_col.get(key, "")
            tf_val = tf_col.get(key, "")
            if py_val != tf_val:
                py_name = py_col.get("name", f"#{i}")
                findings.append(
                    f"system_columns[{i}] ({py_name}): {key!r} diverges\n"
                    f"  Python: {py_val!r}\n"
                    f"  Terraform: {tf_val!r}"
                )
    # If one side has extra trailing entries, surface them.
    for extra_i in range(min(len(py), len(tf)), max(len(py), len(tf))):
        if extra_i >= len(py):
            findings.append(
                f"system_columns[{extra_i}]: only in Terraform — " f"{tf[extra_i].get('name')!r}"
            )
        else:
            findings.append(
                f"system_columns[{extra_i}]: only in Python — " f"{py[extra_i].get('name')!r}"
            )
    return findings


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(repo_root: Path) -> int:
    py_src_path = repo_root / PY_SCHEMA_MAPPING
    tf_src_path = repo_root / TF_REPLICA_TABLES
    if not py_src_path.exists():
        sys.stderr.write(f"ERROR: Python file not found: {py_src_path}\n")
        return 2
    if not tf_src_path.exists():
        sys.stderr.write(f"ERROR: Terraform file not found: {tf_src_path}\n")
        return 2

    try:
        py_source = py_src_path.read_text()
        py_type_map_v = python_type_map(py_source)
        py_sys_cols = python_system_columns(py_source)
    except (SyntaxError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"ERROR parsing {py_src_path}: {exc}\n")
        return 2

    try:
        tf_source = tf_src_path.read_text()
        tf_type_map_v = terraform_type_map(tf_source)
        tf_sys_cols = terraform_system_columns(tf_source)
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"ERROR parsing {tf_src_path}: {exc}\n")
        return 2

    findings: list[str] = []
    findings.extend(diff_type_maps(py_type_map_v, tf_type_map_v))
    findings.extend(diff_system_columns(py_sys_cols, tf_sys_cols))

    if not findings:
        print(
            "✓ schema_mapping alignment OK: "
            f"{len(py_type_map_v)} type entries + "
            f"{len(py_sys_cols)} system columns match across Python and Terraform."
        )
        return 0

    print(f"⚠ schema_mapping alignment FAILED: {len(findings)} drift finding(s)")
    print()
    print(f"Python source:    {PY_SCHEMA_MAPPING}")
    print(f"Terraform source: {TF_REPLICA_TABLES}")
    print()
    for f in findings:
        print(f"  • {f}")
    print()
    print(
        "Fix: update whichever side is missing the change. Both must declare "
        "every Airtable type the sync supports and every system column appended "
        "to the replica tables, in the same order."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        repo_root = Path(args[0]).resolve()
    else:
        repo_root = Path(__file__).resolve().parents[1]
    return run(repo_root)


if __name__ == "__main__":
    sys.exit(main())
