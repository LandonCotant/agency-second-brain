"""Unit tests for `ProjectResolver` (ADR 0019).

Covers the algorithm: exact match wins, domain match needs same-account
guard, free-mail domains skip, non-Gmail sources skip, TTL refresh on
warm instance.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.triage.models import Source
from agency_brain.agents.triage.project_resolver import (
    DEFAULT_TTL_SECONDS,
    FREE_MAIL_DOMAINS,
    ProjectResolver,
)

# --------------------------------------------------------------- test doubles


class _StubBQ:
    """Records SQL queries and returns canned rows. Each call to query_rows
    is independent — the resolver loads once per refresh, so test cases that
    exercise TTL flip the rows mid-test by replacing self._rows."""

    def __init__(self, rows: list[dict]) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def query_rows(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        return list(self._rows)

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows


class _Clock:
    """Manual clock for TTL tests — `advance(n)` moves time forward n seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _row(**kwargs) -> dict:
    """Build a fixture row with sensible defaults; override per test."""
    base = {
        "sender_email": "alice@acme.com",
        "sender_domain": "acme.com",
        "account_id": "recCli01",
        "project_id": "recProj01",
        "project_phase": "Build",
        "project_last_modified": datetime(2026, 4, 28, tzinfo=UTC),
        "owner_email": "owner@example.com",
        "owner_user_id": "usrthe operator",
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------- tests


def test_exact_match_returns_project_with_owner_user_id() -> None:
    bq = _StubBQ([_row()])
    resolver = ProjectResolver(bq_client=bq, project_id="agency-brain-demo")

    result = resolver.resolve("alice@acme.com", source=Source.GMAIL)

    assert result is not None
    assert result.project_record_id == "recProj01"
    assert result.owner_user_id == "usrthe operator"
    assert result.account_id == "recCli01"
    assert result.match_reason == "exact"
    # SQL was issued exactly once at init.
    assert len(bq.queries) == 1
    assert "sender_to_project_v" in bq.queries[0]


def test_sender_normalization_handles_case_and_whitespace() -> None:
    bq = _StubBQ([_row(sender_email="alice@acme.com")])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    # Mixed case + leading whitespace shouldn't break exact match.
    result = resolver.resolve("  Alice@ACME.com", source=Source.GMAIL)
    assert result is not None and result.project_record_id == "recProj01"


def test_exact_match_picks_build_phase_over_discovery() -> None:
    """Tiebreaker: phase priority breaks Build/Launch ahead of Discovery."""
    bq = _StubBQ(
        [
            _row(project_id="recDiscovery", project_phase="Discovery"),
            _row(project_id="recBuild", project_phase="Build"),
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    result = resolver.resolve("alice@acme.com", source=Source.GMAIL)
    assert result is not None and result.project_record_id == "recBuild"


def test_exact_match_recency_breaks_phase_ties() -> None:
    """Two same-phase candidates → most-recently-modified wins."""
    bq = _StubBQ(
        [
            _row(
                project_id="recOlder",
                project_phase="Build",
                project_last_modified=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            _row(
                project_id="recNewer",
                project_phase="Build",
                project_last_modified=datetime(2026, 4, 28, tzinfo=UTC),
            ),
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    result = resolver.resolve("alice@acme.com", source=Source.GMAIL)
    assert result is not None and result.project_record_id == "recNewer"


def test_domain_match_returns_when_only_one_client_at_domain() -> None:
    bq = _StubBQ(
        [
            _row(sender_email="alice@acme.com", project_id="recAcme01"),
            _row(sender_email="bob@acme.com", project_id="recAcme01"),  # same project
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    # Carol@acme.com isn't in the contact list; domain match should still fire
    # because every candidate at acme.com belongs to client recCli01.
    result = resolver.resolve("carol@acme.com", source=Source.GMAIL)
    assert result is not None
    assert result.match_reason == "domain"
    assert result.project_record_id == "recAcme01"


def test_domain_match_skipped_when_multiple_clients_at_domain() -> None:
    """Same-client guard (HIPAA-safe). Two clients at one domain → no match."""
    bq = _StubBQ(
        [
            _row(
                sender_email="alice@shared.com",
                account_id="recClientA",
                project_id="recProjA",
            ),
            _row(
                sender_email="bob@shared.com",
                account_id="recClientB",
                project_id="recProjB",
            ),
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    # Carol@shared.com — no exact, ambiguous domain.
    result = resolver.resolve("carol@shared.com", source=Source.GMAIL)
    assert result is None


def test_free_mail_domain_never_falls_back_to_domain_match() -> None:
    """gmail.com et al. would route every personal address to one project — wrong."""
    # Even if the view inadvertently has a row for someone@gmail.com,
    # a different gmail address must NOT fall back to it.
    bq = _StubBQ(
        [
            _row(
                sender_email="alice@gmail.com",
                sender_domain="gmail.com",
                project_id="recRandomProject",
            )
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    result = resolver.resolve("bob@gmail.com", source=Source.GMAIL)
    assert result is None
    # But an EXACT match on alice@gmail.com still works — exact is high-trust.
    exact = resolver.resolve("alice@gmail.com", source=Source.GMAIL)
    assert exact is not None and exact.project_record_id == "recRandomProject"


def test_non_gmail_source_returns_none_without_querying() -> None:
    bq = _StubBQ([_row()])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    queries_before = len(bq.queries)

    # Exhaustive coverage of every non-Gmail Source enum:
    for source in (
        Source.DRIVE,
        Source.AIRTABLE,
        Source.CALENDAR,
        Source.CHAT,
        Source.VANTAGE,
    ):
        assert resolver.resolve("alice@acme.com", source=source) is None

    # No additional queries issued — early return.
    assert len(bq.queries) == queries_before


def test_empty_sender_returns_none() -> None:
    bq = _StubBQ([_row()])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    assert resolver.resolve("", source=Source.GMAIL) is None


def test_sender_without_at_sign_skips_domain_lookup() -> None:
    bq = _StubBQ([_row()])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    assert resolver.resolve("not-an-email", source=Source.GMAIL) is None


def test_resolver_handles_empty_view_gracefully() -> None:
    bq = _StubBQ([])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    assert resolver.resolve("alice@acme.com", source=Source.GMAIL) is None


def test_resolver_skips_malformed_rows_missing_required_keys() -> None:
    """Schema-drift defense: rows without project_id/sender_email don't crash.

    Build a view where every row in the dataset is malformed — the resolver
    should ignore them all and return None rather than crash on a missing
    key. (A mixed-validity dataset would still allow domain match through
    the valid rows; that's tested separately.)
    """
    bq = _StubBQ(
        [
            {"sender_email": None, "project_id": "recX", "account_id": "recCli01"},
            {"sender_email": "alice@acme.com", "project_id": None, "account_id": "recCli01"},
            {"sender_email": "bob@acme.com", "project_id": "recBob", "account_id": None},
        ]
    )
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    # Every row was malformed — resolver loaded zero valid entries.
    assert resolver.resolve("alice@acme.com", source=Source.GMAIL) is None
    assert resolver.resolve("bob@acme.com", source=Source.GMAIL) is None


def test_ttl_refresh_re_queries_view_after_expiry() -> None:
    clock = _Clock(start=0.0)
    bq = _StubBQ([_row(sender_email="alice@acme.com", project_id="recOld")])
    resolver = ProjectResolver(
        bq_client=bq,
        project_id="proj",
        ttl_seconds=3600,
        clock=clock,
    )
    assert len(bq.queries) == 1  # initial load

    # Within TTL — no refresh.
    clock.advance(60)
    resolver.resolve("alice@acme.com", source=Source.GMAIL)
    assert len(bq.queries) == 1

    # Mutate the underlying view + jump past TTL.
    bq.set_rows([_row(sender_email="alice@acme.com", project_id="recNew")])
    clock.advance(3600)
    result = resolver.resolve("alice@acme.com", source=Source.GMAIL)
    assert len(bq.queries) == 2
    assert result is not None and result.project_record_id == "recNew"


def test_default_ttl_is_one_hour() -> None:
    assert DEFAULT_TTL_SECONDS == 3600


def test_free_mail_domains_includes_common_providers() -> None:
    """Sanity: the set is non-empty and covers gmail/outlook/yahoo at minimum."""
    for domain in ("gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"):
        assert domain in FREE_MAIL_DOMAINS


def test_owner_user_id_is_none_when_team_join_misses() -> None:
    """LEFT JOIN on Team — projects whose owner has no Team row yield NULL."""
    bq = _StubBQ([_row(owner_user_id=None)])
    resolver = ProjectResolver(bq_client=bq, project_id="proj")
    result = resolver.resolve("alice@acme.com", source=Source.GMAIL)
    assert result is not None
    assert result.owner_user_id is None
    # Project still resolves; agent will draft without explicit Owner.
    assert result.project_record_id == "recProj01"
