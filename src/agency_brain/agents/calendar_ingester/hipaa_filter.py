"""HIPAA filter for the Calendar ingester.

Drops events whose attendees or organizer include a HIPAA-flagged
client domain (PRD §4.1 layer 3). Reuses the ``HipaaFilter``-shaped
domain check the CRM Auto-updater introduced (ADR 0047), but
defined here to keep the calendar ingester independently importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import CalendarEvent


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class HipaaPreflightResult:
    allowed: bool
    blocking_attendees: tuple[str, ...]


class CalendarHipaaFilter:
    def __init__(self, hipaa_domains: tuple[str, ...]) -> None:
        self._domains = frozenset(d.lower() for d in hipaa_domains if d)

    @property
    def domains(self) -> frozenset[str]:
        return self._domains

    def check(self, event: CalendarEvent) -> HipaaPreflightResult:
        offenders: list[str] = []
        for addr in (event.organizer, *event.attendees):
            if not addr:
                continue
            domain = addr.split("@", 1)[-1] if "@" in addr else addr
            if domain in self._domains:
                offenders.append(addr)
        return HipaaPreflightResult(
            allowed=not offenders,
            blocking_attendees=tuple(offenders),
        )


def load_hipaa_domains_from_bq(
    *,
    bq_query: BQQueryClient,
    project_id: str,
    accounts_table: str = "accounts",
    dataset_id: str = "airtable_replica",
) -> tuple[str, ...]:
    """Same shape as ``crm_updater.hipaa_filter.load_hipaa_domains_from_bq``."""
    table_ref = f"{project_id}.{dataset_id}.{accounts_table}"
    sql = f"SELECT DISTINCT google_group_email, website FROM `{table_ref}` WHERE IFNULL(hipaa, FALSE) = TRUE"  # noqa: S608, E501
    try:
        rows = bq_query.query_rows(sql)
    except Exception:
        return ()
    domains: set[str] = set()
    for row in rows:
        for candidate in (row.get("google_group_email"), row.get("website")):
            d = _domain_of(candidate or "")
            if d:
                domains.add(d)
    return tuple(sorted(domains))


def _domain_of(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.strip().lower()
    if "@" in addr:
        return addr.rsplit("@", 1)[-1]
    if "://" in addr:
        addr = addr.split("://", 1)[1]
    addr = addr.split("/", 1)[0].split("?", 1)[0]
    if addr.startswith("www."):
        addr = addr[4:]
    return addr
