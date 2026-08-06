"""WS-G3 Morning Brief input + output dataclasses.

Inputs are constructed by the Cloud Run Job entrypoint (one per recipient
per execution). Outputs mirror the columns of `agent_outputs.morning_briefs`
declared in `terraform/modules/agent_runtime/main.tf`. The writer in
`writer.py` adds `brief_id`, `agent_run_id`, `model`, `latency_ms`,
`success`, and `error` — those are not produced by the LLM.

Per ADR 0029: drafts-only (PRD §4.7); never auto-sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class CalendarEvent:
    """One event from the recipient's primary calendar for `local_date`."""

    summary: str
    start: datetime
    end: datetime
    attendees: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriagedItemSnippet:
    """Shape rendered into the Top 3 + Rest of Queue sections."""

    item_id: str
    severity: str
    summary: str
    source: str
    source_url: str | None = None


@dataclass(frozen=True)
class OpenTaskSnippet:
    """Open Airtable Task assigned to the recipient."""

    task_id: str
    name: str
    due_date: date | None
    project_name: str | None = None


@dataclass(frozen=True)
class RiskFlagSnippet:
    """Risk Watcher flag scoped to one of the recipient's accounts."""

    flag_id: str
    severity: str
    pattern_name: str
    account_name: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class DraftAwaitingReviewSnippet:
    """Airtable Task with Approval Status = 'Drafted by Agent'."""

    task_id: str
    name: str
    project_name: str | None = None
    drafted_at: datetime | None = None


@dataclass(frozen=True)
class MorningBriefInput:
    """One brief invocation = one recipient on one local date."""

    recipient_email: str
    run_date: date
    """Calendar date the brief covers (in BRIEF_TIMEZONE)."""
    aspects: list[str] = field(default_factory=list)
    """Knowledge Catalog aspects. Triggers BaseAgent HIPAA pre-flight when
    `hipaa_excluded` is present. Empty for v1; future per-account briefs may
    set this when the recipient's queue includes HIPAA-flagged accounts.
    """


@dataclass(frozen=True)
class MorningBriefOutput:
    """Composed brief + dispatch outcome.

    Mirrors `agent_outputs.morning_briefs` columns. The writer adds the
    bookkeeping columns that aren't on this dataclass.
    """

    brief_id: str
    recipient_email: str
    local_date: date
    body_markdown: str
    sections_used: tuple[str, ...]
    prompt_version: str
    confidence: float = 1.0
    """BaseAgent requires a confidence in [0.0, 1.0]. The brief is composed
    by an LLM but the rendering itself is deterministic; confidence here
    reflects whether all data sources returned cleanly. Default 1.0 — readers
    that fail set this to 0.6 to route to human review per BaseAgent.
    """
    gmail_draft_id: str | None = None
    dedup_skipped: bool = False
    dedup_existing_brief_id: str | None = None
