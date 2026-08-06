"""HIPAA pre-flight filter (ADR 0047 §7).

Rejects any email whose sender or recipients include a HIPAA-flagged
client domain. The HIPAA domain set is loaded from
``airtable_replica.accounts WHERE hipaa = true`` at run start.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .models import GmailMessage

log = logging.getLogger("agency_brain.agents.crm_updater.hipaa_filter")


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class HipaaPreflightResult:
    allowed: bool
    blocking_addresses: tuple[str, ...]


class HipaaFilter:
    """Rejects emails whose participants include HIPAA-flagged domains."""

    def __init__(self, *, hipaa_domains: Iterable[str]) -> None:
        self._domains = frozenset(d.lower() for d in hipaa_domains if d)

    @property
    def domains(self) -> frozenset[str]:
        return self._domains

    def check(self, message: GmailMessage) -> HipaaPreflightResult:
        addrs = (
            message.from_addr,
            *message.to_addrs,
            *message.cc_addrs,
        )
        offenders: list[str] = []
        for addr in addrs:
            if not addr:
                continue
            domain = _domain_of(addr)
            if not domain:
                continue
            if domain in self._domains:
                offenders.append(addr)
        return HipaaPreflightResult(
            allowed=not offenders,
            blocking_addresses=tuple(offenders),
        )


def load_hipaa_domains_from_bq(
    *,
    bq_query: BQQueryClient,
    project_id: str,
    accounts_table: str = "accounts",
    dataset_id: str = "airtable_replica",
) -> tuple[str, ...]:
    """Read HIPAA-flagged account domains from `airtable_replica.accounts`.

    Today there are zero HIPAA accounts (per CLAUDE.md), so this query
    typically returns []. The filter is forward-defense — flipping a
    single Account.HIPAA checkbox should immediately exclude the matching
    domain from extraction.
    """
    table_ref = f"{project_id}.{dataset_id}.{accounts_table}"
    sql = f"SELECT DISTINCT google_group_email, website FROM `{table_ref}` WHERE IFNULL(hipaa, FALSE) = TRUE"  # noqa: S608, E501
    try:
        rows = bq_query.query_rows(sql)
    except Exception:
        log.exception("crm_updater.hipaa_filter.load_failed")
        return ()
    domains: set[str] = set()
    for row in rows:
        email = row.get("google_group_email") or ""
        site = row.get("website") or ""
        for candidate in (email, site):
            d = _domain_of(candidate)
            if d:
                domains.add(d)
    return tuple(sorted(domains))


def _domain_of(addr: str) -> str:
    """Pull the lowercase domain from an email or URL string. Empty if unparseable."""
    if not addr:
        return ""
    addr = addr.strip().lower()
    if "@" in addr:
        return addr.rsplit("@", 1)[-1].split(">", 1)[0].strip()
    # URL form
    if "://" in addr:
        addr = addr.split("://", 1)[1]
    addr = addr.split("/", 1)[0].split("?", 1)[0]
    if addr.startswith("www."):
        addr = addr[4:]
    return addr
