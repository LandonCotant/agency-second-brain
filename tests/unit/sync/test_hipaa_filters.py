"""HIPAA filterByFormula clauses are exact strings — assert them verbatim."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agency_brain.sync.hipaa_filters import (
    HIPAA_FILTERS,
    hipaa_filter_for,
)


def test_hipaa_filters_cover_every_replicated_table():
    # Single-base architecture (ADR 0020). Captures added per ADR 0039.
    # Orchestrator Inbox added per audit finding F7 (2026-05-28).
    assert set(HIPAA_FILTERS.keys()) == {
        "Accounts",
        "Contacts",
        "Contracts",
        "Projects",
        "Tasks",
        "Team",
        "Goals",
        "Goal Scores",
        "Risk Profiles",
        "Service Catalog",
        "Captures",
        "Orchestrator Inbox",
    }


def test_hipaa_filters_match_schema_json_tables():
    """Future-proofs against the 2026-05-14 Captures regression: every
    table the sync iterates from airtable/schema.json must have a
    HIPAA_FILTERS entry. Without this assertion, adding a new table to
    schema.json silently breaks the sync at runtime with a KeyError."""
    schema_path = Path(__file__).resolve().parents[3] / "airtable" / "schema.json"
    schema = json.loads(schema_path.read_text())
    # schema["tables"] is a list of bare table-name strings.
    schema_table_names = set(schema["tables"])
    assert schema_table_names == set(HIPAA_FILTERS.keys()), (
        "schema.json tables and HIPAA_FILTERS keys diverged — "
        f"missing from HIPAA_FILTERS: {schema_table_names - set(HIPAA_FILTERS)}; "
        f"extra in HIPAA_FILTERS: {set(HIPAA_FILTERS) - schema_table_names}"
    )


def test_accounts_filter_is_negation_of_hipaa_checkbox():
    """Accounts is the HIPAA root post-collapse (ADR 0020). Uses the
    explicit ``= TRUE()`` form like every other HIPAA-bearing table so a
    blank/absent checkbox can't evaluate NOT(blank) into over-exclusion."""
    assert hipaa_filter_for("Accounts") == "NOT({HIPAA} = TRUE())"


def test_contacts_and_contracts_filter_via_account_lookup():
    """Both inherit HIPAA from Accounts via the Account HIPAA lookup."""
    assert hipaa_filter_for("Contacts") == "NOT({Account HIPAA} = TRUE())"
    assert hipaa_filter_for("Contracts") == "NOT({Account HIPAA} = TRUE())"


def test_projects_filter_uses_account_hipaa_lookup():
    # ADR 0020: Project.Client HIPAA → Project.Account HIPAA after collapse.
    assert hipaa_filter_for("Projects") == "NOT({Account HIPAA} = TRUE())"


def test_tasks_filter_uses_transitive_lookup():
    # Tasks.Project HIPAA still chains through Project, but its lookup
    # target is now Account HIPAA (ADR 0020). The filter formula is
    # unchanged on the Tasks side because the field name stayed `Project HIPAA`.
    assert hipaa_filter_for("Tasks") == "NOT({Project HIPAA} = TRUE())"


def test_unrelated_tables_get_permissive_filter():
    for table in (
        "Team",
        "Goals",
        "Goal Scores",
        "Risk Profiles",
        "Service Catalog",
        "Captures",
    ):
        assert hipaa_filter_for(table) == "TRUE()"


def test_unknown_table_raises():
    with pytest.raises(KeyError):
        hipaa_filter_for("Mystery")
