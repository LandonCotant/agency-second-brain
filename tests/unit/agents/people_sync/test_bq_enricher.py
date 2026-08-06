"""Tests for ``people_sync.bq_enricher`` — Account AUTO section content from BQ."""

from __future__ import annotations

from agency_brain.agents.people_sync.bq_enricher import AccountEnricher
from agency_brain.agents.people_sync.sections import AUTO_MARKER


class _FakeBQ:
    def __init__(
        self, *, by_substring: dict[str, list[dict]] | None = None, raise_on: str | None = None
    ) -> None:
        self._by_sub = by_substring or {}
        self._raise_on = raise_on
        self.queries: list[tuple[str, list[dict] | None]] = []

    def query_rows(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if self._raise_on and self._raise_on in sql:
            raise RuntimeError("BQ boom")
        for key, rows in self._by_sub.items():
            if key in sql:
                return list(rows)
        return []


def test_active_engagements_lists_open_projects_with_wikilinks() -> None:
    bq = _FakeBQ(
        by_substring={
            "airtable_replica.projects": [
                {
                    "project_name": "Q3 Campaign",
                    "status": "Active",
                    "health": None,
                    "target_end_date": "2026-09-30",
                },
                {
                    "project_name": "Site Redesign",
                    "status": "Blocked",
                    "health": "Yellow",
                    "target_end_date": None,
                },
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.active_engagements(airtable_id="recA1")
    assert out.startswith(AUTO_MARKER)
    # No health → no parens
    assert "[[Q3 Campaign]] — Active — ends 2026-09-30" in out
    # Health present → "(<health> health)" between status and end-date
    assert "[[Site Redesign]] — Blocked (Yellow health)" in out
    # SQL parameters propagated
    sql, params = bq.queries[0]
    assert params == [{"name": "account_id", "type": "STRING", "value": "recA1"}]
    # Health column requested
    assert "p.health" in sql


def test_active_engagements_empty_returns_placeholder() -> None:
    bq = _FakeBQ()
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.active_engagements(airtable_id="recA1")
    assert AUTO_MARKER in out
    assert "(no active engagements)" in out


def test_active_engagements_query_failure_does_not_crash() -> None:
    bq = _FakeBQ(raise_on="airtable_replica.projects")
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.active_engagements(airtable_id="recA1")
    assert AUTO_MARKER in out
    assert "(query failed; will retry next tick)" in out


def test_recent_activity_uses_both_account_name_and_id_params() -> None:
    bq = _FakeBQ(
        by_substring={
            "note_hits": [
                {
                    "activity_date": "2026-05-17",
                    "source": "email",
                    "line": "Re: Q3 strategy review",
                },
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.recent_activity(account_name="Client A", airtable_id="recA1")
    assert "2026-05-17 — Re: Q3 strategy review" in out
    sql, params = bq.queries[0]
    by_name = {p["name"]: p["value"] for p in params}
    assert by_name["account_name_like"] == "%Client A%"
    assert by_name["account_id"] == "recA1"
    # SQL UNIONs note_hits + risk_hits and excludes galaxy self-references.
    assert "note_hits" in sql
    assert "risk_hits" in sql
    assert "'galaxy'" not in sql  # galaxy excluded from the kind whitelist


def test_recent_activity_surfaces_risk_flag_summary() -> None:
    """Risk hits show up as 'SEVERITY: pattern — N firings since DATE' lines."""
    bq = _FakeBQ(
        by_substring={
            "risk_hits": [
                {
                    "activity_date": "2026-05-19",
                    "source": "risk_flag",
                    "line": "CRITICAL: Owner Disengagement — 9 firings since 2026-05-11",
                },
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.recent_activity(account_name="Client A", airtable_id="recA1")
    assert "2026-05-19 — CRITICAL: Owner Disengagement — 9 firings since 2026-05-11" in out


def test_recent_activity_empty_returns_placeholder() -> None:
    bq = _FakeBQ()
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.recent_activity(account_name="Quiet Client", airtable_id="recQ")
    assert "(no recent activity in last 30 days)" in out


def test_open_risks_single_firing_renders_with_flagged_date() -> None:
    """firing_count=1 → '(flagged <date>)' format, not the collapsed summary."""
    bq = _FakeBQ(
        by_substring={
            "agent_outputs.risk_flags": [
                {
                    "pattern_name": "Owner Disengagement",
                    "severity": "critical",
                    "firing_count": 1,
                    "first_flagged_at": "2026-05-07 12:00:00",
                    "last_flagged_at": "2026-05-07 12:00:00",
                    "signal_evidence": "Zero meetings, emails, or triaged items in the last 30 days.",
                }
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.open_risks(airtable_id="recA1")
    assert "**critical** Owner Disengagement (flagged 2026-05-07 12:00:00)" in out
    assert "Zero meetings" in out
    sql, params = bq.queries[0]
    assert {"name": "account_id", "type": "STRING", "value": "recA1"} in params
    # SQL uses account_id FK, not name match, and GROUP BYs pattern/severity
    assert "r.account_id = @account_id" in sql
    assert "GROUP BY r.pattern_name, r.severity" in sql


def test_open_risks_collapses_repeat_firings_into_count_and_range() -> None:
    """firing_count>1 → 'N firings, first → last' format."""
    bq = _FakeBQ(
        by_substring={
            "agent_outputs.risk_flags": [
                {
                    "pattern_name": "Owner Disengagement",
                    "severity": "critical",
                    "firing_count": 9,
                    "first_flagged_at": "2026-05-11 13:01:51",
                    "last_flagged_at": "2026-05-19 13:02:47",
                    "signal_evidence": "No engagement in last 30 days.",
                }
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.open_risks(airtable_id="recA1")
    assert "**critical** Owner Disengagement (9 firings, 2026-05-11 → 2026-05-19)" in out


def test_open_risks_truncates_long_evidence() -> None:
    long_evidence = "A" * 200
    bq = _FakeBQ(
        by_substring={
            "agent_outputs.risk_flags": [
                {
                    "pattern_name": "Test",
                    "severity": "medium",
                    "firing_count": 1,
                    "first_flagged_at": "2026-05-01 00:00:00",
                    "last_flagged_at": "2026-05-01 00:00:00",
                    "signal_evidence": long_evidence,
                }
            ]
        }
    )
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.open_risks(airtable_id="recX")
    # Truncated at 100 chars + ellipsis
    assert "AAAAA" in out
    assert "…" in out
    # The full 200-A string should not appear
    assert long_evidence not in out


def test_open_risks_empty_returns_placeholder() -> None:
    bq = _FakeBQ()
    enricher = AccountEnricher(bq_query=bq, project_id="p")
    out = enricher.open_risks(airtable_id="recHealthy")
    assert "(no open risks)" in out


# --------------------------------------------------------------- ContactEnricher (Phase 4)


from agency_brain.agents.people_sync.bq_enricher import ContactEnricher


def test_conversation_log_groups_by_date() -> None:
    bq = _FakeBQ(
        by_substring={
            "calendar_hits": [
                {"log_date": "2026-05-18", "line": "meeting: Quarterly sync"},
                {"log_date": "2026-05-10", "line": "mention: morning brief 2026-05-10"},
            ]
        }
    )
    enricher = ContactEnricher(bq_query=bq, project_id="p")
    out = enricher.conversation_log(contact_name="Sam Q", email="sam@example.com")
    assert AUTO_MARKER in out
    assert "### 2026-05-18" in out
    assert "### 2026-05-10" in out
    out_lines = out.splitlines()
    assert any("meeting: Quarterly sync" in line for line in out_lines)


def test_conversation_log_empty_returns_placeholder() -> None:
    bq = _FakeBQ()
    enricher = ContactEnricher(bq_query=bq, project_id="p")
    out = enricher.conversation_log(contact_name="Quiet Person", email="qp@x.com")
    assert "(no conversation traces in last 90 days)" in out


def test_conversation_log_handles_null_email() -> None:
    """Some contacts have no email; the query should still run (no triage/calendar
    hits but wikilink mentions could still surface)."""
    bq = _FakeBQ(
        by_substring={
            "wikilink_hits": [
                {"log_date": "2026-05-15", "line": "mention: weekly review 2026-05-12"},
            ]
        }
    )
    enricher = ContactEnricher(bq_query=bq, project_id="p")
    out = enricher.conversation_log(contact_name="Emailless Person", email=None)
    assert "### 2026-05-15" in out
    sql, params = bq.queries[0]
    # The email param should be normalized to empty string, not None
    email_param = next(p for p in params if p["name"] == "email")
    assert email_param["value"] == ""


def test_conversation_log_query_failure_does_not_crash() -> None:
    bq = _FakeBQ(raise_on="calendar_hits")
    enricher = ContactEnricher(bq_query=bq, project_id="p")
    out = enricher.conversation_log(contact_name="X", email="x@x.com")
    assert "(query failed; will retry next tick)" in out


def test_conversation_log_sql_includes_wikilink_pattern() -> None:
    """The wikilink branch matches markdown_content containing [[Contact Name]]."""
    bq = _FakeBQ()
    enricher = ContactEnricher(bq_query=bq, project_id="p")
    enricher.conversation_log(contact_name="Sam Q", email="sam@x.com")
    sql, params = bq.queries[0]
    assert "wikilink_hits" in sql
    assert "[[" in sql and "]]" in sql
    name_param = next(p for p in params if p["name"] == "contact_name")
    assert name_param["value"] == "Sam Q"
