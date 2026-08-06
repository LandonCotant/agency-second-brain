"""Output writers for the Triage Agent.

Two destinations per PRD §6.3 + spec §5.1:
- `agent_outputs.triaged_items` (BQ-canonical, always written)
- `airtable_replica.tasks` (Airtable-materialized draft, only when
  action_type != do_now AND we can resolve a project link)

Both writers are injectable so unit tests stay fast (no real BQ/Airtable)
and PR 4's Reasoning Engine entrypoint can swap clients without touching
agent logic.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .models import ActionType, TriageInput, TriageOutput


class BQRowsClient(Protocol):
    """Minimal BQ surface — matches `google.cloud.bigquery.Client.insert_rows_json`."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQDedupClient(Protocol):
    """Minimal BQ surface for the dedup SELECT (ADR 0026).

    Implementations must accept a parameterized query (input_hash + window
    minutes) and return the existing ``item_id`` or ``None``. SELECT against
    streaming-buffer rows is allowed by BigQuery — only DML hits the
    streaming-buffer wall (see ADR 0025) — so this works on rows seconds old.
    """

    def find_recent_item_id_by_hash(
        self, table_ref: str, input_hash: str, window_minutes: int
    ) -> str | None: ...


class AirtableTasksClient(Protocol):
    """Minimal Airtable surface — POST a Tasks row, return the created record id."""

    def create_task(self, fields: dict) -> str:
        """Create one row, return its `rec...` id."""
        ...


# ---------------------------------------------------------------- BQ writer


@dataclass(frozen=True)
class TriagedItemRow:
    """The 24-column row written to `agent_outputs.triaged_items`."""

    item_id: str
    triaged_at: str  # ISO 8601 UTC
    agent_run_id: str
    source: str
    input_hash: str
    actionable: bool
    owner_type: str
    action_type: str
    severity: str
    confidence: float
    human_review_routed: bool
    reasoning: str
    model: str
    prompt_version: str
    positive_goal_achieving: str | None = None
    owner_email: str | None = None
    category: str | None = None
    task_or_project: str | None = None
    source_url: str | None = None
    source_event_ref: str | None = None
    ingested_at: str | None = None
    airtable_task_record_id: str | None = None
    account_id: str | None = None

    def to_bq_row(self) -> dict:
        return {
            "item_id": self.item_id,
            "triaged_at": self.triaged_at,
            "agent_run_id": self.agent_run_id,
            "source": self.source,
            "input_hash": self.input_hash,
            "actionable": self.actionable,
            "owner_type": self.owner_type,
            "action_type": self.action_type,
            "severity": self.severity,
            "confidence": self.confidence,
            "human_review_routed": self.human_review_routed,
            "reasoning": self.reasoning,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "positive_goal_achieving": self.positive_goal_achieving,
            "owner_email": self.owner_email,
            "category": self.category,
            "task_or_project": self.task_or_project,
            "source_url": self.source_url,
            "source_event_ref": self.source_event_ref,
            "ingested_at": self.ingested_at,
            "airtable_task_record_id": self.airtable_task_record_id,
            "account_id": self.account_id,
        }


class TriagedItemWriteError(RuntimeError):
    pass


CONFIDENCE_THRESHOLD = 0.7  # mirrors BaseAgent.CONFIDENCE_THRESHOLD


class TriagedItemWriter:
    """Streaming insert into `agent_outputs.triaged_items`.

    `agent_run_id` is required by the schema and documented as joining to
    `agent_audit_log.events.event_id`. The BaseAgent currently generates the
    audit event_id internally per invocation; until that's threaded through
    (follow-up), TriageAgent generates a single `run_id` per `_run()` call
    and uses it here. The audit row's event_id will be different in v1 — the
    join is recoverable via `(triaged_at, source, source_event_ref)`.

    ADR 0026: also exposes ``find_recent_by_hash`` for input-hash dedup.
    The dedup SELECT uses a separate ``BQDedupClient`` (typically the same
    underlying ``google.cloud.bigquery.Client`` adapted) so unit tests can
    stub it independently of the insert path.
    """

    def __init__(
        self,
        *,
        bq_client: BQRowsClient,
        project_id: str,
        dataset_id: str = "agent_outputs",
        table_id: str = "triaged_items",
        dedup_client: BQDedupClient | None = None,
    ) -> None:
        self._bq = bq_client
        self._table_ref = f"{project_id}.{dataset_id}.{table_id}"
        self._dedup = dedup_client

    def write(
        self,
        *,
        input: TriageInput,
        output: TriageOutput,
        run_id: str,
        model: str,
        prompt_version: str,
        airtable_task_record_id: str | None = None,
        account_id: str | None = None,
    ) -> str:
        """Insert one row. Returns `item_id` so the caller can reference it."""
        row = build_triaged_item_row(
            input=input,
            output=output,
            run_id=run_id,
            model=model,
            prompt_version=prompt_version,
            airtable_task_record_id=airtable_task_record_id,
            account_id=account_id,
        )
        errors = self._bq.insert_rows_json(self._table_ref, [row.to_bq_row()])
        if errors:
            raise TriagedItemWriteError(f"BQ rejected triaged_items insert: {errors}")
        return row.item_id

    def find_recent_by_hash(self, input_hash: str, window_minutes: int) -> str | None:
        """Return existing ``item_id`` for ``input_hash`` within window, or None.

        ADR 0026: dedup pre-check before the writer chain runs. Returns the
        most recent matching ``item_id`` if any row matches; ``None`` when
        no dedup client is configured (test path) or no match within window.
        """
        if self._dedup is None:
            return None
        return self._dedup.find_recent_item_id_by_hash(self._table_ref, input_hash, window_minutes)


def build_triaged_item_row(
    *,
    input: TriageInput,
    output: TriageOutput,
    run_id: str,
    model: str,
    prompt_version: str,
    airtable_task_record_id: str | None = None,
    account_id: str | None = None,
) -> TriagedItemRow:
    return TriagedItemRow(
        item_id=str(uuid.uuid4()),
        triaged_at=datetime.now(UTC).isoformat(),
        agent_run_id=run_id,
        source=input.source.value,
        input_hash=compute_input_hash(input),
        actionable=output.actionable,
        owner_type=output.owner_type.value,
        action_type=output.action_type.value,
        severity=output.severity.value,
        confidence=output.confidence,
        human_review_routed=output.confidence < CONFIDENCE_THRESHOLD,
        reasoning=output.reasoning,
        model=model,
        prompt_version=prompt_version,
        positive_goal_achieving=(
            output.positive_goal_achieving.value if output.positive_goal_achieving else None
        ),
        owner_email=output.owner_email,
        category=output.category.value if output.category else None,
        task_or_project=output.task_or_project.value if output.task_or_project else None,
        source_url=input.source_url or None,
        source_event_ref=input.source_event_ref or None,
        ingested_at=input.ingested_at.isoformat(),
        airtable_task_record_id=airtable_task_record_id,
        account_id=account_id,
    )


def compute_input_hash(input: TriageInput) -> str:
    """SHA-256 of normalized input. Stable dedup key for re-classification."""
    normalized = "|".join(
        [
            input.source.value,
            input.source_url or "",
            input.source_event_ref or "",
            input.body,
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ----------------------------------------------------------- Airtable writer


_ACTION_TYPE_DISPLAY = {
    ActionType.DO_NOW: "Do It Now",
    ActionType.DELEGATE: "Delegate",
    ActionType.DEFER: "Defer",
    ActionType.SCHEDULE: "Schedule",
    ActionType.WAIT: "Wait",
}

_CATEGORY_DISPLAY = {
    "calls": "Calls",
    "computer": "Computer",
    "errands": "Errands",
    "office": "Office",
    "schedule": "Schedule",
    "team_meeting": "Team Meeting",
    "staff": "Staff",
    "waiting_for": "Waiting For",
    "home": "Home",
}


class TaskDraftWriteError(RuntimeError):
    pass


class TaskDrafter:
    """Creates an Airtable Tasks row with `Source = 'Triage Agent'` and
    `Approval Status = 'Drafted by Agent'` (PRD §4.7 drafts boundary).

    Skipped entirely for `action_type = do_now` (no Airtable row needed —
    the operator acts immediately) and when `project_record_id` is None.
    """

    def __init__(self, *, airtable: AirtableTasksClient) -> None:
        self._airtable = airtable

    def draft(
        self,
        *,
        input: TriageInput,
        output: TriageOutput,
        project_record_id: str,
        owner_user_id: str | None = None,
    ) -> str:
        if output.action_type is ActionType.DO_NOW:
            raise TaskDraftWriteError(
                "TaskDrafter must not be called for action_type=do_now (spec §5.1)"
            )
        fields = build_task_fields(
            input=input,
            output=output,
            project_record_id=project_record_id,
            owner_user_id=owner_user_id,
        )
        try:
            return self._airtable.create_task(fields)
        except Exception as exc:
            raise TaskDraftWriteError(f"Airtable Tasks create failed: {exc}") from exc


def build_task_fields(
    *,
    input: TriageInput,
    output: TriageOutput,
    project_record_id: str,
    owner_user_id: str | None,
) -> dict:
    """Field shape per `airtable/schema.json` Tasks table.

    ``owner_user_id`` is the Airtable user collaborator id (``usrXXX``),
    surfaced via ``sender_to_project_v.owner_user_id`` (which joins
    Operations.Team.User by workspace email — ADR 0019). Airtable's
    singleCollaborator field accepts the bare id string on POST.
    """
    fields: dict = {
        "Task Name": _build_task_name(input),
        "Source": "Triage Agent",
        "Approval Status": "Drafted by Agent",
        "Status": "Open",
        "Action Type": _ACTION_TYPE_DISPLAY[output.action_type],
        "Project": [project_record_id],
        "Source Reference": input.source_url or input.source_event_ref,
        "Task Type": (
            "Project"
            if output.task_or_project and output.task_or_project.value == "project"
            else "Task"
        ),
    }
    if owner_user_id:
        fields["Owner"] = owner_user_id
    if output.category:
        fields["Category"] = _CATEGORY_DISPLAY[output.category.value]
    return fields


def _build_task_name(input: TriageInput) -> str:
    # Only the subject (operator-visible, comparatively trusted) becomes the
    # Task Name. When it's blank, fall back to a neutral label rather than
    # the raw first line of the body: the body is attacker-controlled
    # (inbound email) and ADR 0017 disables Model Armor, so unmediated body
    # text must not surface as a human-facing field.
    base = input.subject.strip()
    if len(base) > 200:
        base = base[:197] + "..."
    return base or f"[{input.source.value}] untitled signal"
