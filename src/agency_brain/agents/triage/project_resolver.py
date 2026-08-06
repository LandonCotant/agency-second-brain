"""Sender → Project resolver for the Triage Agent (ADR 0019).

Reads ``airtable_replica.sender_to_project_v`` (created in PR1) once at agent
init, refreshes every hour, and resolves a Gmail signal's sender to an
Operations Project record id so the agent can draft an Airtable Task under
the right Project.

Algorithm (per ADR 0019):
1. Skip non-Gmail sources — Slack/Calendar/Vantage signals don't have
   email senders the resolver can reason about.
2. Exact email match wins. Multiple candidates → Phase tiebreaker
   (Build/Launch > others) then recency (``project_last_modified DESC``).
3. Domain match only when no exact match AND the sender's domain is not
   a free-mail domain (gmail.com etc.) AND every candidate Active project
   shares the same ``account_id``. The same-account guard is HIPAA-safe:
   the view filters HIPAA accounts out at the source, but a HIPAA account
   and a non-HIPAA account could share a domain — domain match across
   accounts would silently leak a HIPAA-account signal to a different
   account's project.
4. Anything else → return None. Caller routes to the Triage Inbox sentinel
   project (``TB_TRIAGE_INBOX_PROJECT_ID``) and emits a
   ``triage.no_project_match`` audit event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .goal_context import BQQueryClient
from .models import Source

# Free-mail domains never enter the domain pool. A match against gmail.com
# would route every contact at every personal gmail to whatever single
# active project happens to be at gmail.com — almost certainly wrong.
FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "aol.com",
    }
)

# Phase tiebreaker: active-delivery phases beat early/late ones when a
# account has multiple Active projects. Mirrors airtable/schema.json
# Projects.Phase options ordering by "where signal traffic is loudest".
_PHASE_PRIORITY: dict[str, int] = {
    "Build": 0,
    "Launch": 0,
    "Design": 1,
    "Maintain": 1,
    "Discovery": 2,
    "Closeout": 2,
}
_DEFAULT_PHASE_PRIORITY = 3  # unrecognized phases sort last

DEFAULT_TTL_SECONDS = 3600  # 1 hour — Reasoning Engine instances stay warm
# past the daily MV refresh; resolver must re-read.


@dataclass(frozen=True)
class ResolverRow:
    """One materialized-view row, parsed."""

    sender_email: str
    sender_domain: str
    account_id: str
    project_id: str
    project_phase: str | None
    project_last_modified: datetime | None
    owner_email: str | None
    owner_user_id: str | None  # populated via Team.User join (ADR 0019)


@dataclass(frozen=True)
class ResolverResult:
    project_record_id: str
    owner_user_id: str | None
    account_id: str
    match_reason: Literal["exact", "domain"]


_RESOLVER_SQL = """
SELECT
  sender_email,
  sender_domain,
  account_id,
  project_id,
  project_phase,
  project_last_modified,
  owner_email,
  owner_user_id
FROM `{project}.airtable_replica.sender_to_project_v`
WHERE COALESCE(hipaa_excluded, FALSE) = FALSE
"""


class ProjectResolver:
    """Eager-loaded sender lookup with TTL refresh.

    Instantiated once per Reasoning Engine instance in ``agent.set_up()``
    and held for the instance lifetime. Each ``resolve()`` checks staleness
    and reloads from BQ if the TTL has expired — keeps a warm instance
    from drifting past the materialized view's daily refresh.
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: callable = time.monotonic,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._by_email: dict[str, list[ResolverRow]] = {}
        self._by_domain: dict[str, list[ResolverRow]] = {}
        self._loaded_at: float = 0.0
        self._load()

    # ----------------------------------------------------------- public API

    def resolve(self, sender: str, *, source: Source) -> ResolverResult | None:
        """Resolve a sender to a project, or None if no safe match."""
        if source is not Source.GMAIL:
            return None
        if not sender:
            return None

        self._refresh_if_stale()

        normalized = sender.strip().lower()

        exact_candidates = self._by_email.get(normalized, [])
        if exact_candidates:
            winner = _pick_winner(exact_candidates)
            return _to_result(winner, "exact")

        # No exact match — try domain.
        if "@" not in normalized:
            return None
        domain = normalized.rsplit("@", 1)[1]
        if not domain or domain in FREE_MAIL_DOMAINS:
            return None

        domain_candidates = self._by_domain.get(domain, [])
        if not domain_candidates:
            return None
        # Same-account guard: ambiguous across accounts → no match (HIPAA-safe).
        account_ids = {row.account_id for row in domain_candidates}
        if len(account_ids) > 1:
            return None
        winner = _pick_winner(domain_candidates)
        return _to_result(winner, "domain")

    # ----------------------------------------------------------- internals

    def _refresh_if_stale(self) -> None:
        if self._clock() - self._loaded_at >= self._ttl_seconds:
            self._load()

    def _load(self) -> None:
        sql = _RESOLVER_SQL.format(project=self._project_id)
        rows = self._bq.query_rows(sql)
        by_email: dict[str, list[ResolverRow]] = {}
        by_domain: dict[str, list[ResolverRow]] = {}
        for raw in rows:
            row = _row_from_dict(raw)
            if row is None:
                continue
            by_email.setdefault(row.sender_email, []).append(row)
            by_domain.setdefault(row.sender_domain, []).append(row)
        self._by_email = by_email
        self._by_domain = by_domain
        self._loaded_at = self._clock()


def _row_from_dict(raw: dict) -> ResolverRow | None:
    """Parse one BQ row dict into a ResolverRow, or None if malformed.

    The view filters NULL emails at the source, but defend in depth so a
    schema-drift surprise doesn't crash the agent.
    """
    sender_email = raw.get("sender_email")
    project_id = raw.get("project_id")
    account_id = raw.get("account_id")
    if not sender_email or not project_id or not account_id:
        return None
    return ResolverRow(
        sender_email=str(sender_email).lower(),
        sender_domain=str(raw.get("sender_domain") or "").lower(),
        account_id=str(account_id),
        project_id=str(project_id),
        project_phase=(str(raw["project_phase"]) if raw.get("project_phase") else None),
        project_last_modified=raw.get("project_last_modified"),
        owner_email=(str(raw["owner_email"]) if raw.get("owner_email") else None),
        owner_user_id=(str(raw["owner_user_id"]) if raw.get("owner_user_id") else None),
    )


def _pick_winner(candidates: list[ResolverRow]) -> ResolverRow:
    """Phase-then-recency tiebreaker. Stable for single-element lists."""
    if len(candidates) == 1:
        return candidates[0]

    def sort_key(row: ResolverRow) -> tuple:
        phase_priority = _PHASE_PRIORITY.get(row.project_phase or "", _DEFAULT_PHASE_PRIORITY)
        # Recency: more-recently-modified projects win. Compare via
        # negative timestamp seconds when present; absent → sort last.
        recency_key: float
        if row.project_last_modified is None:
            recency_key = 0.0
        else:
            recency_key = -row.project_last_modified.timestamp()
        return (phase_priority, recency_key, row.project_id)

    return sorted(candidates, key=sort_key)[0]


def _to_result(row: ResolverRow, match_reason: Literal["exact", "domain"]) -> ResolverResult:
    return ResolverResult(
        project_record_id=row.project_id,
        owner_user_id=row.owner_user_id,
        account_id=row.account_id,
        match_reason=match_reason,
    )
