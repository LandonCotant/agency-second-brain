"""Tests for ``HipaaFilter`` and the BQ loader."""

from __future__ import annotations

from datetime import UTC, datetime

from agency_brain.agents.crm_updater.hipaa_filter import (
    HipaaFilter,
    _domain_of,
    load_hipaa_domains_from_bq,
)
from agency_brain.agents.crm_updater.models import GmailMessage


def _msg(*, frm: str, to: tuple[str, ...] = (), cc: tuple[str, ...] = ()) -> GmailMessage:
    return GmailMessage(
        message_id="m",
        thread_id="t",
        subject="x",
        from_addr=frm,
        to_addrs=to,
        cc_addrs=cc,
        body_text="",
        received_at=datetime.now(UTC),
    )


def test_check_allows_when_no_hipaa_domains() -> None:
    f = HipaaFilter(hipaa_domains=())
    result = f.check(_msg(frm="anyone@example.com"))
    assert result.allowed is True
    assert result.blocking_addresses == ()


def test_check_blocks_sender_in_hipaa_domain() -> None:
    f = HipaaFilter(hipaa_domains=("hospitalcorp.com",))
    msg = _msg(frm="someone@hospitalcorp.com", to=("owner@example.com",))
    result = f.check(msg)
    assert result.allowed is False
    assert "someone@hospitalcorp.com" in result.blocking_addresses


def test_check_blocks_recipient_in_hipaa_domain() -> None:
    f = HipaaFilter(hipaa_domains=("hospitalcorp.com",))
    msg = _msg(
        frm="ok@example.com",
        to=("contact@hospitalcorp.com",),
    )
    result = f.check(msg)
    assert result.allowed is False
    assert "contact@hospitalcorp.com" in result.blocking_addresses


def test_check_blocks_cc_in_hipaa_domain() -> None:
    f = HipaaFilter(hipaa_domains=("hospitalcorp.com",))
    msg = _msg(
        frm="ok@example.com",
        to=("ok2@example.com",),
        cc=("watcher@hospitalcorp.com",),
    )
    result = f.check(msg)
    assert result.allowed is False


def test_check_case_insensitive_domain_match() -> None:
    f = HipaaFilter(hipaa_domains=("HospitalCorp.COM",))
    msg = _msg(frm="x@hospitalcorp.com")
    result = f.check(msg)
    assert result.allowed is False


# ------------------------------------------------------------- _domain_of


def test_domain_of_email() -> None:
    assert _domain_of("alice@example.com") == "example.com"
    assert _domain_of("Alice@Example.COM") == "example.com"


def test_domain_of_url() -> None:
    assert _domain_of("https://www.example.com/path?q=1") == "example.com"
    assert _domain_of("http://example.com") == "example.com"


def test_domain_of_empty() -> None:
    assert _domain_of("") == ""
    assert _domain_of(None or "") == ""


# ----------------------------------------------------------- BQ loader


class _FakeBQ:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.last_sql: str | None = None

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.last_sql = sql
        return list(self.rows)


def test_load_hipaa_domains_from_bq_extracts_unique_domains() -> None:
    bq = _FakeBQ(
        rows=[
            {
                "google_group_email": "alerts@hospitalcorp.com",
                "website": "https://hospitalcorp.com",
            },
            {"google_group_email": None, "website": "https://www.healthplus.org"},
            {"google_group_email": "share@hospitalcorp.com", "website": ""},
        ]
    )
    domains = load_hipaa_domains_from_bq(bq_query=bq, project_id="p")
    assert "hospitalcorp.com" in domains
    assert "healthplus.org" in domains
    assert len(domains) == 2  # deduped


def test_load_hipaa_domains_returns_empty_on_failure() -> None:
    class _Boom:
        def query_rows(self, sql, parameters=None):
            raise RuntimeError("boom")

    domains = load_hipaa_domains_from_bq(bq_query=_Boom(), project_id="p")
    assert domains == ()
