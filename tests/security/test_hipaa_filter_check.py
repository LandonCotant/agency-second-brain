"""Unit tests for scripts/hipaa_filter_check.py."""

from __future__ import annotations

from pathlib import Path

import hipaa_filter_check as hfc

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sql"


def _check(name: str) -> str | None:
    return hfc.file_violation(FIXTURES / name, FIXTURES.parent.parent.parent)


def test_clean_select_passes():
    assert _check("clean_select.sql") is None


def test_qualified_clause_passes():
    """Regression: ``COALESCE(p.hipaa_excluded, FALSE) = FALSE`` (with table
    alias prefix) must also pass — multi-table JOINs across replica tables
    that both have a hipaa_excluded column need the qualifier. Surfaced
    2026-05-28 by the audit's account_id backfill SQL."""
    assert _check("qualified_clause.sql") is None


def test_missing_clause_fails():
    v = _check("missing_clause.sql")
    assert v is not None
    assert "canonical clause" in v


def test_missing_marker_fails():
    v = _check("missing_marker.sql")
    assert v is not None
    assert "marker comment" in v


def test_comment_only_mention_passes():
    # The clients/projects mention is in comments only; after stripping,
    # no client-data table is referenced.
    assert _check("comment_only_mention.sql") is None


def test_block_comment_only_passes():
    assert _check("block_comment_only.sql") is None


def test_ddl_only_passes():
    # CREATE TABLE / DROP INDEX only. No SELECT, so no clause required.
    assert _check("ddl_only.sql") is None


def test_view_with_select_fails():
    v = _check("view_with_select.sql")
    assert v is not None


def test_view_v_table_passes():
    # client_owners_v matches the expanded `*_v` view regex; satisfies marker
    # + clause, so passes.
    assert _check("view_v_table.sql") is None


def test_full_scan_skips_fixtures(tmp_path: Path):
    # find_violations(repo_root) should NOT walk into tests/fixtures/.
    # Simulate by creating a sibling fixtures path under tmp_path.
    fixtures_dir = tmp_path / "tests" / "fixtures" / "sql"
    fixtures_dir.mkdir(parents=True)
    bad = fixtures_dir / "intentionally_bad.sql"
    bad.write_text("SELECT * FROM clients;\n")

    assert hfc.find_violations(tmp_path) == []


def test_full_scan_finds_real_violation(tmp_path: Path):
    bad_dir = tmp_path / "src"
    bad_dir.mkdir()
    bad = bad_dir / "bad.sql"
    bad.write_text("SELECT * FROM airtable_replica.clients;\n")

    violations = hfc.find_violations(tmp_path)
    assert len(violations) == 1
    assert "src/bad.sql" in violations[0]


def test_strip_comments_unit():
    raw = "SELECT 1 -- mentions clients\nFROM /* projects */ t;"
    stripped = hfc._strip_comments(raw)
    assert "clients" not in stripped
    assert "projects" not in stripped
    assert "FROM" in stripped
