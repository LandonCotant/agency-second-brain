"""Unit tests for the Airtable schema drift audit module.

Designed to catch the Phase 0 bug pattern that's surfaced four times in
2026-05: schema.json says REQUIRED, live row is blank, sync 400s on next
WRITE_TRUNCATE. The ``operational_drift`` test below is the load-bearing
regression guard.

W3 hardening (2026-05-28): the pure-logic functions moved from
``scripts/check_airtable_schema_drift.py`` to
``src/agency_brain/audit/airtable_schema_drift.py`` so the manual CLI
script and the nightly Cloud Run Job share one source of truth. Tests
import from the audit module directly.
"""

from __future__ import annotations

from agency_brain.audit.airtable_schema_drift import (
    Drift,
    build_blank_field_formula,
    diff_metadata,
    drift_summary,
    format_report,
    normalize_declared_schema,
    operational_drift,
    required_fields_by_table,
    resolve_schema_path,
)

# ---------------------------------------------------------------------------
# normalize_declared_schema
# ---------------------------------------------------------------------------


def test_normalize_declared_skips_lookup_fields():
    """Lookup fields aren't replicated to BQ — they're filtered out of the
    drift check too to avoid false positives when Airtable returns them
    differently from the lookup spec in schema.json."""
    schema = {
        "tables": {
            "Projects": {
                "fields": [
                    {"name": "Project Name", "type": "singleLineText", "required": True},
                    {
                        "name": "Account HIPAA",
                        "type": "multipleLookupValues",
                        "required": True,
                    },
                ]
            }
        }
    }
    out = normalize_declared_schema(schema)
    assert set(out["Projects"]) == {"Project Name"}


def test_normalize_declared_captures_required_flag():
    schema = {
        "tables": {
            "Tasks": {
                "fields": [
                    {"name": "Source", "type": "singleSelect", "required": True},
                    {"name": "Notes", "type": "multilineText"},
                ]
            }
        }
    }
    out = normalize_declared_schema(schema)
    assert out["Tasks"]["Source"] == {"type": "singleSelect", "required": True}
    assert out["Tasks"]["Notes"] == {"type": "multilineText", "required": False}


# ---------------------------------------------------------------------------
# diff_metadata
# ---------------------------------------------------------------------------


def test_diff_metadata_clean_when_schemas_match():
    declared = {"Tasks": {"Source": {"type": "singleSelect", "required": True}}}
    live = {"Tasks": {"Source": "singleSelect"}}
    assert diff_metadata(declared, live) == []


def test_diff_metadata_flags_missing_table():
    declared = {"Tasks": {}, "Goals": {}}
    live = {"Tasks": {}}
    drifts = diff_metadata(declared, live)
    assert [d.category for d in drifts] == ["missing_table"]
    assert drifts[0].table == "Goals"


def test_diff_metadata_flags_new_table():
    declared = {"Tasks": {}}
    live = {"Tasks": {}, "Surprise": {}}
    drifts = diff_metadata(declared, live)
    assert [d.category for d in drifts] == ["new_table"]
    assert drifts[0].table == "Surprise"


def test_diff_metadata_flags_missing_field():
    declared = {"Tasks": {"Source": {"type": "singleSelect", "required": True}}}
    live = {"Tasks": {}}
    drifts = diff_metadata(declared, live)
    assert [d.category for d in drifts] == ["missing_field"]
    assert drifts[0].field == "Source"


def test_diff_metadata_flags_type_change():
    """Singleselect → multilineText would silently lose enum semantics."""
    declared = {"Tasks": {"Source": {"type": "singleSelect", "required": True}}}
    live = {"Tasks": {"Source": "multilineText"}}
    drifts = diff_metadata(declared, live)
    assert [d.category for d in drifts] == ["type_change"]
    assert "declared=singleSelect" in drifts[0].detail
    assert "live=multilineText" in drifts[0].detail


# ---------------------------------------------------------------------------
# required_fields_by_table
# ---------------------------------------------------------------------------


def test_required_fields_by_table_only_required():
    declared = {
        "Tasks": {
            "Source": {"type": "singleSelect", "required": True},
            "Notes": {"type": "multilineText", "required": False},
            "Action Type": {"type": "singleSelect", "required": True},
        }
    }
    assert required_fields_by_table(declared) == {"Tasks": ["Action Type", "Source"]}


def test_required_fields_by_table_skips_tables_with_no_required_fields():
    declared = {"Logs": {"Note": {"type": "multilineText", "required": False}}}
    assert required_fields_by_table(declared) == {}


def test_required_fields_by_table_skips_checkbox_fields():
    """Pins the 2026-05-28 false-positive fix: ``Accounts.HIPAA`` is declared
    REQUIRED + checkbox; the first live run flagged 3 unchecked rows as
    'sync will 400' but checkboxes map blank→BOOL false in BQ, never NULL.
    Skip checkbox fields in the operational drift list to avoid the noise.
    """
    declared = {
        "Accounts": {
            "HIPAA": {"type": "checkbox", "required": True},
            "Name": {"type": "singleLineText", "required": True},
        }
    }
    assert required_fields_by_table(declared) == {"Accounts": ["Name"]}


# ---------------------------------------------------------------------------
# build_blank_field_formula
# ---------------------------------------------------------------------------


def test_build_blank_field_formula_wraps_field_in_braces():
    """Airtable formula syntax uses {Field Name} for braced refs; the script
    must wrap each declared field name regardless of spaces/special chars."""
    formula = build_blank_field_formula("Action Type")
    assert formula == "AND({Action Type} = BLANK())"


def test_build_blank_field_formula_escapes_single_quote():
    formula = build_blank_field_formula("Owner's Note")
    assert "Owner\\'s Note" in formula


# ---------------------------------------------------------------------------
# operational_drift — the Phase 0 regression guard
# ---------------------------------------------------------------------------


def test_operational_drift_no_blank_rows_returns_empty():
    """Steady-state: every REQUIRED field is populated on every live row."""
    required = {"Tasks": ["Source", "Action Type"]}
    fetcher_calls: list[tuple[str, str]] = []

    def fetcher(table: str, formula: str) -> list[dict]:
        fetcher_calls.append((table, formula))
        return []

    assert operational_drift(required, fetcher) == []
    # Verify it actually called Airtable for every REQUIRED field.
    assert len(fetcher_calls) == 2
    assert {f for _, f in fetcher_calls} == {
        "AND({Source} = BLANK())",
        "AND({Action Type} = BLANK())",
    }


def test_operational_drift_flags_blank_row():
    """The exact bug class from PR #170 (Tasks.Source NULLABLE):
    schema.json says Source is REQUIRED, a human created a row leaving it blank,
    next sync 400s. This test pins that the drift script catches it BEFORE
    the next sync run."""
    required = {"Tasks": ["Source"]}

    def fetcher(table: str, formula: str) -> list[dict]:
        return [
            {"id": "recBlankRow1", "fields": {"Task Name": "incomplete"}},
            {"id": "recBlankRow2", "fields": {"Task Name": "also incomplete"}},
        ]

    drifts = operational_drift(required, fetcher)
    assert len(drifts) == 1
    d = drifts[0]
    assert d.category == "required_violation"
    assert d.table == "Tasks"
    assert d.field == "Source"
    assert "recBlankRow1" in d.detail
    assert "recBlankRow2" in d.detail
    assert "2 live row(s)" in d.detail


def test_operational_drift_emits_per_field_not_per_row():
    """One drift entry per (table, field), regardless of how many blank rows
    that field has. Keeps the report concise even on widespread regressions."""
    required = {"Tasks": ["Source", "Action Type"]}

    def fetcher(table: str, formula: str) -> list[dict]:
        if "Source" in formula:
            return [{"id": f"recS{i}"} for i in range(5)]
        if "Action Type" in formula:
            return [{"id": f"recA{i}"} for i in range(3)]
        return []

    drifts = operational_drift(required, fetcher)
    assert len(drifts) == 2
    fields = {d.field for d in drifts}
    assert fields == {"Source", "Action Type"}


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_clean():
    out = format_report([])
    assert "No drift detected" in out


def test_format_report_groups_by_category_with_count():
    drifts = [
        Drift(category="required_violation", table="Tasks", field="Source", detail="x"),
        Drift(category="required_violation", table="Tasks", field="Action Type", detail="x"),
        Drift(category="new_field", table="Accounts", field="Untracked Col", detail="x"),
    ]
    out = format_report(drifts)
    assert "3 drift finding(s)" in out
    assert "[required_violation]  (2)" in out
    assert "[new_field]  (1)" in out
    # Required violations come first (the Phase 0 class is highest priority).
    assert out.index("[required_violation]") < out.index("[new_field]")


def test_format_report_renders_field_qualification():
    drifts = [Drift(category="missing_field", table="Tasks", field="Source", detail="x")]
    out = format_report(drifts)
    assert "Tasks.Source" in out


# ---------------------------------------------------------------------------
# drift_summary (audit-Job payload shape)
# ---------------------------------------------------------------------------


def test_drift_summary_clean():
    summary = drift_summary([])
    assert summary == {"finding_count": 0, "by_category": {}, "samples": []}


def test_drift_summary_groups_and_caps_samples():
    """``DriftReporter.drift()`` serializes this dict to the BQ row's
    ``output`` column. Cap samples so a runaway drift doesn't bloat the
    row beyond BQ's STRING column comfort zone.
    """
    drifts = [
        Drift(category="required_violation", table="Tasks", field=f"f{i}", detail="x")
        for i in range(20)
    ]
    drifts.append(Drift(category="new_field", table="Accounts", field="x", detail="y"))
    summary = drift_summary(drifts)
    assert summary["finding_count"] == 21
    assert summary["by_category"] == {"required_violation": 20, "new_field": 1}
    assert len(summary["samples"]) == 10
    # Sample shape is a JSON-safe dict, not the Drift dataclass.
    assert summary["samples"][0] == {
        "category": "required_violation",
        "table": "Tasks",
        "field": "f0",
        "detail": "x",
    }


# ---------------------------------------------------------------------------
# resolve_schema_path
# ---------------------------------------------------------------------------


def test_resolve_schema_path_explicit_wins(monkeypatch):
    monkeypatch.setenv("SCHEMA_JSON_PATH", "/env/path.json")
    out = resolve_schema_path("/explicit/path.json")
    assert str(out) == "/explicit/path.json"


def test_resolve_schema_path_env_var_wins_over_default(monkeypatch):
    monkeypatch.setenv("SCHEMA_JSON_PATH", "/env/path.json")
    out = resolve_schema_path()
    assert str(out) == "/env/path.json"


def test_resolve_schema_path_repo_fallback(monkeypatch, tmp_path):
    """When neither explicit nor env var is set AND /app/airtable/schema.json
    doesn't exist (local dev case), the resolver walks up from __file__ to
    the repo-relative path.
    """
    monkeypatch.delenv("SCHEMA_JSON_PATH", raising=False)
    out = resolve_schema_path()
    # Path resolves to <repo_root>/airtable/schema.json. Don't assert exact
    # path because the test runs from the repo root; instead assert shape.
    assert out.name == "schema.json"
    assert out.parent.name == "airtable"
