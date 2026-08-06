"""Triage Agent input + output dataclasses.

The output mirrors the columns of `agent_outputs.triaged_items`
(`terraform/modules/agent_runtime/main.tf`). The writer in `writers.py`
(PR 2) handles `item_id`, `triaged_at`, `agent_run_id`, `input_hash`,
`model`, `prompt_version` — those are not produced by the LLM.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class Source(enum.StrEnum):
    GMAIL = "gmail"
    DRIVE = "drive"
    AIRTABLE = "airtable"
    CALENDAR = "calendar"
    CHAT = "chat"
    VANTAGE = "vantage"


class OwnerType(enum.StrEnum):
    BRIAN = "brian"
    DELEGATE = "delegate"
    NA = "na"


class ActionType(enum.StrEnum):
    DO_NOW = "do_now"
    DELEGATE = "delegate"
    DEFER = "defer"
    SCHEDULE = "schedule"
    WAIT = "wait"


class PGAStrength(enum.StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class Category(enum.StrEnum):
    CALLS = "calls"
    COMPUTER = "computer"
    ERRANDS = "errands"
    OFFICE = "office"
    SCHEDULE = "schedule"
    TEAM_MEETING = "team_meeting"
    STAFF = "staff"
    WAITING_FOR = "waiting_for"
    HOME = "home"


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class TaskOrProject(enum.StrEnum):
    TASK = "task"
    PROJECT = "project"


@dataclass(frozen=True)
class TriageInput:
    """Pub/Sub envelope from `asb-triage-input`. WS-B sync owns the population.

    Triage v1 expects the body to already be present on the message. Future
    iterations may fetch from Gmail/Drive directly via DWD; out of scope here.
    """

    source: Source
    source_url: str
    source_event_ref: str
    sender: str
    subject: str
    body: str
    ingested_at: datetime
    aspects: list[str] = field(default_factory=list)
    """Knowledge Catalog aspects copied from the source. Triggers the BaseAgent
    HIPAA pre-flight when `hipaa_excluded` is present.
    """


@dataclass(frozen=True)
class TriageOutput:
    """Classification result. The writer adds the bookkeeping columns."""

    actionable: bool
    owner_type: OwnerType
    action_type: ActionType
    severity: Severity
    confidence: float
    reasoning: str
    positive_goal_achieving: PGAStrength | None = None
    owner_email: str | None = None
    category: Category | None = None
    task_or_project: TaskOrProject | None = None
    # ADR 0026: when True, this classification was a duplicate of a recent
    # signal (same input_hash within the dedup window) and the writer chain
    # was skipped. The output is still returned so the BaseAgent audit row
    # captures the dedup decision via _summarize_output.
    dedup_skipped: bool = False
    dedup_existing_item_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actionable and self.positive_goal_achieving is not None:
            raise ValueError(
                "positive_goal_achieving must be null when actionable=false "
                "(spec §7.1). Got "
                f"{self.positive_goal_achieving!r}."
            )
        if self.actionable and self.positive_goal_achieving is None:
            raise ValueError(
                "positive_goal_achieving is required when actionable=true " "(spec §7.2)."
            )
        if self.owner_type is OwnerType.NA and self.owner_email is not None:
            raise ValueError("owner_email must be null when owner_type='na'.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}.")
