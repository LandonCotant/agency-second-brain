"""Unit tests for scripts/check_schema_mapping_alignment.py.

The script enforces the W1 hardening from the 2026-05-28 audit. Tests pin
the parsers (Python AST extraction, Terraform regex extraction) and the
diff predicates so a future refactor doesn't silently weaken the gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from check_schema_mapping_alignment import (  # type: ignore  # noqa: E402
    diff_system_columns,
    diff_type_maps,
    python_system_columns,
    python_type_map,
    run,
    terraform_system_columns,
    terraform_type_map,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Python-side parsing
# ---------------------------------------------------------------------------


def test_python_type_map_extracts_real_file():
    """Smoke check: the real schema_mapping.py parses to a non-empty dict."""
    source = (REPO_ROOT / "src/agency_brain/sync/schema_mapping.py").read_text()
    out = python_type_map(source)
    assert isinstance(out, dict)
    # Every entry is a (bq_type, mode) tuple of strings.
    for k, v in out.items():
        assert isinstance(k, str) and isinstance(v, tuple) and len(v) == 2
        assert v[0] in ("STRING", "BOOL", "DATE", "TIMESTAMP", "FLOAT64", "INT64")
        assert v[1] in ("NULLABLE", "REQUIRED", "REPEATED")
    # Known-canonical entries (regression — if any of these disappear, audit
    # the diff against the audit branch).
    assert out["singleLineText"] == ("STRING", "NULLABLE")
    assert out["checkbox"] == ("BOOL", "NULLABLE")
    assert out["richText"] == ("STRING", "NULLABLE")  # added in PR #173
    assert out["autoNumber"] == ("INT64", "NULLABLE")  # added in PR #173


def test_python_system_columns_extracts_real_file():
    source = (REPO_ROOT / "src/agency_brain/sync/schema_mapping.py").read_text()
    out = python_system_columns(source)
    assert isinstance(out, list)
    assert len(out) == 6
    # The first column is the BQ primary key — pinned because order matters.
    assert out[0]["name"] == "_airtable_record_id"
    assert out[0]["mode"] == "REQUIRED"
    # Description concatenated from a parenthesized multi-line literal must
    # collapse to a single string with no source-formatting noise.
    hipaa_col = out[5]
    assert hipaa_col["name"] == "hipaa_excluded"
    assert "COALESCE(hipaa_excluded, FALSE) = FALSE" in hipaa_col["description"]
    assert "\n" not in hipaa_col["description"]


def test_python_type_map_synthetic_source():
    """A minimal handcrafted source is parsed correctly."""
    source = """
_TYPE_MAP: dict[str, tuple[str, str]] = {
    "foo": ("STRING", "NULLABLE"),
    "bar": ("BOOL", "REQUIRED"),
}
"""
    assert python_type_map(source) == {
        "foo": ("STRING", "NULLABLE"),
        "bar": ("BOOL", "REQUIRED"),
    }


# ---------------------------------------------------------------------------
# Terraform-side parsing
# ---------------------------------------------------------------------------


def test_terraform_type_map_extracts_real_file():
    source = (REPO_ROOT / "terraform/modules/data_pipeline/replica_tables.tf").read_text()
    out = terraform_type_map(source)
    assert isinstance(out, dict)
    assert out["singleLineText"] == ("STRING", "NULLABLE")
    assert out["richText"] == ("STRING", "NULLABLE")
    assert out["autoNumber"] == ("INT64", "NULLABLE")


def test_terraform_system_columns_extracts_real_file():
    source = (REPO_ROOT / "terraform/modules/data_pipeline/replica_tables.tf").read_text()
    out = terraform_system_columns(source)
    assert len(out) == 6
    assert out[0]["name"] == "_airtable_record_id"
    assert out[5]["name"] == "hipaa_excluded"


def test_terraform_type_map_synthetic_source():
    """A minimal handcrafted HCL block parses correctly."""
    source = """
locals {
  airtable_to_bq_type = {
    foo = { type = "STRING", mode = "NULLABLE" }
    bar = { type = "BOOL", mode = "REQUIRED" }
  }
}
"""
    assert terraform_type_map(source) == {
        "foo": ("STRING", "NULLABLE"),
        "bar": ("BOOL", "REQUIRED"),
    }


# ---------------------------------------------------------------------------
# Diff predicates
# ---------------------------------------------------------------------------


def test_diff_type_maps_clean():
    py = {"foo": ("STRING", "NULLABLE")}
    tf = {"foo": ("STRING", "NULLABLE")}
    assert diff_type_maps(py, tf) == []


def test_diff_type_maps_only_in_python():
    """Pins the exact failure class PR #173 hit on the audit branch:
    Python added richText/dateTime/autoNumber; Terraform was unchanged.
    """
    py = {"foo": ("STRING", "NULLABLE"), "richText": ("STRING", "NULLABLE")}
    tf = {"foo": ("STRING", "NULLABLE")}
    findings = diff_type_maps(py, tf)
    assert len(findings) == 1
    assert "'richText'" in findings[0]
    assert "missing from Terraform" in findings[0]


def test_diff_type_maps_only_in_terraform():
    py = {"foo": ("STRING", "NULLABLE")}
    tf = {"foo": ("STRING", "NULLABLE"), "bar": ("INT64", "NULLABLE")}
    findings = diff_type_maps(py, tf)
    assert len(findings) == 1
    assert "'bar'" in findings[0]
    assert "missing from Python" in findings[0]


def test_diff_type_maps_value_diverges():
    py = {"foo": ("STRING", "NULLABLE")}
    tf = {"foo": ("STRING", "REPEATED")}
    findings = diff_type_maps(py, tf)
    assert len(findings) == 1
    assert "diverges" in findings[0]


def test_diff_system_columns_clean():
    py = [{"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"}]
    tf = [{"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"}]
    assert diff_system_columns(py, tf) == []


def test_diff_system_columns_description_drift():
    """The most common failure mode here is the description string subtly
    drifting (extra space, em-dash variant). Pin that the diff catches it.
    """
    py = [{"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "hello"}]
    tf = [{"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "hello "}]
    findings = diff_system_columns(py, tf)
    assert len(findings) == 1
    assert "'description'" in findings[0]


def test_diff_system_columns_order_drift():
    """Order is load-bearing — schema_mapping.py docstring says the order
    must match because replica tables get columns in this exact sequence.
    """
    py = [
        {"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"},
        {"name": "b", "type": "STRING", "mode": "REQUIRED", "description": "y"},
    ]
    tf = [
        {"name": "b", "type": "STRING", "mode": "REQUIRED", "description": "y"},
        {"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"},
    ]
    findings = diff_system_columns(py, tf)
    # Per-row, every key diverges → 8 findings (4 keys × 2 rows).
    assert len(findings) > 0
    assert any("'name'" in f for f in findings)


def test_diff_system_columns_length_mismatch():
    py = [{"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"}]
    tf = [
        {"name": "a", "type": "STRING", "mode": "REQUIRED", "description": "x"},
        {"name": "b", "type": "INT64", "mode": "REQUIRED", "description": "y"},
    ]
    findings = diff_system_columns(py, tf)
    assert any("length mismatch" in f for f in findings)
    assert any("only in Terraform" in f for f in findings)


# ---------------------------------------------------------------------------
# End-to-end: the run() entrypoint
# ---------------------------------------------------------------------------


def test_run_returns_zero_on_real_repo():
    """The current repo state must pass — this is the regression guard."""
    assert run(REPO_ROOT) == 0
