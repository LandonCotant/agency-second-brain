"""Risk Watcher dataclasses + Signal Protocol (ADR 0033).

The ``Flag`` shape mirrors ``agent_outputs.risk_flags`` exactly so the
writer can serialize without a translation layer. ``Severity`` matches
the values WS-D's routing matrix expects (the Chat fan-out and Gmail
draft channels both key off this enum).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class Severity(enum.StrEnum):
    """Mirrors ``agent_outputs.risk_flags.severity`` values + the WS-D
    routing matrix (`src/agency_brain/routing/matrix.py`)."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Segment(enum.StrEnum):
    """Profile-segment label written into ``risk_flags.segment``.

    Matches ``airtable_replica.risk_profiles.segment`` so a flag row
    can be joined back to the profile that produced it.
    """

    ECOMMERCE = "E-commerce"
    LOCAL_SERVICE = "Local Service"
    AGENCY_PARTNER = "Agency Partner"
    PERSONAL = "Personal"


@dataclass(frozen=True)
class ClientState:
    """Snapshot of one account's recent state at tick time.

    Built by ``RiskWatcher._load_client_state`` from Airtable replica +
    Memory Bank baselines + (eventually) Vantage/Shopify federation.
    A ``Signal.evaluate`` call gets exactly one of these and returns
    either a ``Flag`` (signal fired) or ``None`` (signal quiet).
    """

    account_id: str
    """Airtable Accounts.``_airtable_record_id`` (the ``rec...`` id)."""

    account_name: str
    segment: Segment
    project_id: str | None
    """Active project record id when one is unambiguously the focus.

    None when the account has zero or multiple active projects; the
    flag will set ``risk_flags.project_id = NULL`` in that case so
    downstream consumers know to fall back to account-scope routing.
    """

    aspects: tuple[str, ...] = ()
    """Carried for BaseAgent's HIPAA pre-flight (`hipaa_excluded`)."""

    baseline: Mapping[str, Any] = field(default_factory=dict)
    """Memory Bank-loaded baseline at namespace
    ``risk-watcher/{account_id}/baseline``. Read-only inside the
    signal; the watcher updates and writes back at tick-end.
    """

    extras: Mapping[str, Any] = field(default_factory=dict)
    """Per-signal-domain payloads. PR-B fills this with task/email/
    deliverable rollups derived from `airtable_replica.*` and Vantage.
    """


@dataclass(frozen=True)
class Flag:
    """One row destined for ``agent_outputs.risk_flags``.

    Field names mirror the BQ schema 1:1 so the writer can pass this
    straight through ``to_bq_row``. The base class fills in
    ``flag_id``, ``flagged_at``, ``human_review_routed``, ``model``,
    and ``prompt_version`` before insert — signals only return the
    domain content.
    """

    pattern_name: str
    severity: Severity
    account_id: str
    project_id: str | None
    segment: Segment
    signal_evidence: str
    reasoning: str
    confidence: float
    data_sources: tuple[str, ...] = ()
    baseline_snapshot: str | None = None


@dataclass(frozen=True)
class RiskWatcherInput:
    """Per-tick input the agent gets.

    Each tick processes a list of accounts. The orchestrator (the
    Cloud Run Job entrypoint, PR-B) pulls this from
    ``airtable_replica.accounts`` and joins to
    ``airtable_replica.projects`` + Memory Bank.
    """

    states: tuple[ClientState, ...]
    aspects: tuple[str, ...] = ()
    """Tick-level aspects. Empty for normal ticks; if a future caller
    wants to mark an entire backfill tick as `hipaa_excluded` this is
    where the BaseAgent guard would short-circuit it."""


@dataclass(frozen=True)
class RiskWatcherOutput:
    """Per-tick output. Carries the fired flags + dedup metadata."""

    flags: tuple[Flag, ...]
    confidence: float
    """Min confidence across fired flags, or 1.0 when no flag fired.

    BaseAgent's CONFIDENCE_THRESHOLD gate (PRD §6.1, base.py:28) keys
    on this. A quiet tick is a high-confidence "nothing happened" so
    the threshold never auto-routes an empty tick to human review.
    """

    accounts_evaluated: int
    accounts_skipped_dedup: int = 0
    """Accounts where every flag in this tick was suppressed by the
    same-day dedup pre-check. PR-B uses this to surface 'quiet' days
    in the audit summary without writing duplicate BQ rows."""


class Signal(Protocol):
    """One detector. Pure-function-shaped: client state in, optional
    flag out. No I/O — the watcher does the BQ + Memory Bank wiring.

    See PRD §6.4 for the contract:

        class Signal:
            name: str
            severity: Severity
            def evaluate(self, client_state: ClientState) -> Flag | None: ...
    """

    name: str
    severity: Severity

    def evaluate(self, client_state: ClientState) -> Flag | None: ...


@dataclass(frozen=True)
class Profile:
    """Bundle of signals for a segment.

    PR-A ships an empty e-commerce profile. PR-B fills the tuple.
    """

    segment: Segment
    signals: tuple[Signal, ...]


def utc_now() -> datetime:
    """Indirection point so tests can monkeypatch tick-time."""
    from datetime import UTC

    return datetime.now(tz=UTC)
