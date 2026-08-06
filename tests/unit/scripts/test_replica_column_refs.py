"""Tests for the replica-column-reference PR gate (scripts/check_replica_column_refs.py).

Guards the guard: confirms it catches dead column references (the F9-cleanup
regression class) without false-positiving on valid columns, non-replica
tables, or SELECT-aliases — and that the live src tree passes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_replica_column_refs.py"

pytest.importorskip("sqlglot")

_spec = importlib.util.spec_from_file_location("replica_column_refs", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture(scope="module")
def valid():
    return mod.valid_columns_by_table()


def test_flags_qualified_dead_column(valid):
    sql = "SELECT t.created FROM `p.airtable_replica.tasks` t"
    assert ("tasks", "created") in mod.check_sql(sql, valid)


def test_flags_unqualified_dead_column_single_table(valid):
    # The MCP read.py case: unqualified column in a single-table query.
    sql = "SELECT created FROM `p.airtable_replica.tasks`"
    assert ("tasks", "created") in mod.check_sql(sql, valid)


def test_flags_dead_last_modified(valid):
    sql = "SELECT c.last_modified FROM `p.airtable_replica.contacts` c"
    assert ("contacts", "last_modified") in mod.check_sql(sql, valid)


def test_valid_columns_pass(valid):
    assert mod.check_sql("SELECT t.task_name FROM `p.airtable_replica.tasks` t", valid) == []
    # system column
    assert (
        mod.check_sql("SELECT t._airtable_last_modified FROM `p.airtable_replica.tasks` t", valid)
        == []
    )


def test_non_replica_table_is_ignored(valid):
    # agent_outputs has no schema here -> never our opinion.
    assert mod.check_sql("SELECT n.bogus FROM `p.agent_outputs.notes` n", valid) == []


def test_select_alias_in_group_by_not_flagged(valid):
    sql = (
        "SELECT p.account[OFFSET(0)] AS account_id, "
        "MAX(t._airtable_last_modified) AS last_action_at "
        "FROM `x.airtable_replica.tasks` t "
        "JOIN `x.airtable_replica.projects` p "
        "ON t.project[OFFSET(0)] = p._airtable_record_id GROUP BY account_id"
    )
    assert mod.check_sql(sql, valid) == []


def test_unqualified_multitable_is_skipped(valid):
    # Ambiguous: can't attribute an unqualified column -> conservatively skip.
    sql = (
        "SELECT created FROM `x.airtable_replica.tasks` t "
        "JOIN `x.airtable_replica.projects` p "
        "ON t.project[OFFSET(0)] = p._airtable_record_id"
    )
    assert mod.check_sql(sql, valid) == []


def test_live_src_tree_passes():
    """The shipped codebase must have zero dead replica-column references."""
    assert mod.main(["check", str(REPO_ROOT / "src" / "agency_brain")]) == 0
