"""Unit tests for the DWD scope half of drafts_boundary_check.

The IAM-side `find_violations` is exercised indirectly by the existing
`tests/security/test_least_privilege_check.py` (same predicate). These
tests cover the new doc-driven DWD allowlist logic added per ADR 0027.
"""

from __future__ import annotations

from agency_brain.audit.drafts_boundary_check import (
    find_dwd_violations,
    parse_dwd_scopes,
)

_HEADER = (
    "| Service Account | Scope | Justification | Granted Date | Workstream |\n"
    "|---|---|---|---|---|\n"
)


def _doc(*rows: str) -> str:
    return "# DWD\n\n" + _HEADER + "\n".join(rows) + "\n"


def test_clean_doc_passes():
    doc = _doc("| `asb-agent-triage-sa` | `gmail.compose` | drafts | 2026-05-01 | WS-G1 |")
    scopes = parse_dwd_scopes(doc)
    assert scopes == [{"sa": "asb-agent-triage-sa", "scope": "gmail.compose"}]
    assert find_dwd_violations(scopes) == []


def test_drift_row_fails():
    doc = _doc(
        "| `asb-agent-triage-sa` | `gmail.compose` | drafts | 2026-05-01 | WS-G1 |",
        "| `asb-agent-rogue-sa`  | `gmail.send`    | bad     | 2026-05-02 | WS-X  |",
    )
    scopes = parse_dwd_scopes(doc)
    violations = find_dwd_violations(scopes)
    assert violations == [{"sa": "asb-agent-rogue-sa", "scope": "gmail.send"}]


def test_send_scope_fails():
    """ADR 0047 admits gmail.modify (label-apply for the CRM Auto-updater)
    but ``gmail.send`` remains forbidden — the drafts-only boundary
    (PRD §4.7) is non-negotiable.
    """
    doc = _doc("| `asb-agent-triage-sa` | `gmail.send` | drafts | 2026-05-01 | WS-G1 |")
    violations = find_dwd_violations(parse_dwd_scopes(doc))
    assert len(violations) == 1
    assert violations[0]["scope"] == "gmail.send"


def test_modify_scope_now_admitted_after_adr_0047():
    """Regression guard: gmail.modify is on the allowlist post-ADR 0047."""
    doc = _doc(
        "| `asb-agent-triage-sa` | `gmail.modify` | drafts | 2026-05-09 | CRM Auto-updater |"
    )
    violations = find_dwd_violations(parse_dwd_scopes(doc))
    assert violations == []


def test_calendar_readonly_passes():
    """ADR 0029: calendar.readonly is on the allowlist alongside gmail.compose."""
    doc = _doc(
        "| `asb-agent-triage-sa` | `gmail.compose`     | drafts | 2026-05-01 | WS-G1 |",
        "| `asb-agent-triage-sa` | `calendar.readonly` | brief  | 2026-05-02 | WS-G3 |",
    )
    scopes = parse_dwd_scopes(doc)
    assert len(scopes) == 2
    assert find_dwd_violations(scopes) == []


def test_other_scope_still_fails_after_calendar_added():
    """Calendar in the allowlist must not relax the rest of the check."""
    doc = _doc(
        "| `asb-agent-triage-sa` | `gmail.compose`     | drafts | 2026-05-01 | WS-G1 |",
        "| `asb-agent-triage-sa` | `calendar.readonly` | brief  | 2026-05-02 | WS-G3 |",
        "| `asb-agent-rogue-sa`  | `gmail.send`        | bad    | 2026-05-03 | WS-X  |",
    )
    violations = find_dwd_violations(parse_dwd_scopes(doc))
    assert violations == [{"sa": "asb-agent-rogue-sa", "scope": "gmail.send"}]


def test_calendar_events_scope_fails():
    """`calendar` (read-write) is NOT on the allowlist; only `calendar.readonly` is."""
    doc = _doc("| `asb-agent-triage-sa` | `calendar` | rw | 2026-05-02 | WS-X |")
    violations = find_dwd_violations(parse_dwd_scopes(doc))
    assert len(violations) == 1
    assert violations[0]["scope"] == "calendar"


def test_empty_table_tolerated():
    doc = _doc("| _(none yet)_ | | | | |")
    scopes = parse_dwd_scopes(doc)
    assert scopes == []
    assert find_dwd_violations(scopes) == []


def test_no_table_at_all_tolerated():
    doc = "# DWD\n\nNo grants documented yet.\n"
    scopes = parse_dwd_scopes(doc)
    assert scopes == []
    assert find_dwd_violations(scopes) == []


def test_real_dwd_scopes_md_parses_clean():
    """Smoke: the real docs/dwd_scopes.md in this repo must pass.

    Post-ADR 0047 there are four rows: gmail.compose, calendar.readonly,
    gmail.readonly, gmail.modify (the latter two for the CRM Auto-updater).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    doc = (repo_root / "docs" / "dwd_scopes.md").read_text(encoding="utf-8")
    scopes = parse_dwd_scopes(doc)
    assert len(scopes) >= 4
    assert find_dwd_violations(scopes) == []
