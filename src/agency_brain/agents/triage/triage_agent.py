"""WS-G1 Triage Agent. PRD §6.3, spec §5.1 + §7.

The LLM client is injected via constructor — keeps unit tests fast and
host-independent. PR 4 wires the real Vertex client + Cached Contents.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Protocol

from ..base import BaseAgent
from .models import (
    ActionType,
    Category,
    OwnerType,
    PGAStrength,
    Severity,
    TaskOrProject,
    TriageInput,
    TriageOutput,
)
from .project_resolver import ProjectResolver, ResolverResult
from .writers import TaskDrafter, TaskDraftWriteError, TriagedItemWriter, compute_input_hash

log = logging.getLogger("agency_brain.agents.triage.triage_agent")

PROMPT_NAME = "triage"
DEFAULT_MODEL = "gemini-2.5-flash"
CONFIDENCE_THRESHOLD = 0.7  # mirrors BaseAgent.CONFIDENCE_THRESHOLD
# ADR 0026: dedup window for input_hash pre-check. 24h covers normal
# Pub/Sub redelivery (≤10 min), ad-hoc retries (≤1h), and manual smoke
# tests, without suppressing legitimate re-classifications across days.
DEFAULT_DEDUP_WINDOW_HOURS = 24


class TriageJSONError(ValueError):
    """LLM returned malformed JSON. Caller (BaseAgent) emits failure audit."""


class TriageClassifierClient(Protocol):
    """Minimal interface for the LLM call.

    PR 4 implements this against Vertex `generate_content` with a
    response_schema for controlled generation. PR 1 stubs it for tests.
    """

    def classify(self, *, prompt: str, signal_block: str) -> str:
        """Return the JSON string the model produced."""
        ...


class GoalContextProvider(Protocol):
    """Pluggable goal-context loader. PR 2 implements this against BQ."""

    def text_block(self) -> str:
        """Return the rendered goal-context block injected into the prompt."""
        ...


class AccountOwnersContextProvider(Protocol):
    """Pluggable account/owner context loader (ADR 0020 — renamed from
    AccountOwnersContextProvider). Implemented against BQ in
    :mod:`goal_context`."""

    def text_block(self) -> str:
        """Return the rendered account+owner reference block."""
        ...


class SenderContextProvider(Protocol):
    """Pluggable sender-contact context loader. Implemented in
    :mod:`sender_context`."""

    def text_block(self, sender_email: str) -> str:
        """Return the rendered sender contact context block."""
        ...


class TriageAgent(BaseAgent[TriageInput, TriageOutput]):
    def __init__(
        self,
        *,
        agent_id: str = "triage",
        sa_email: str,
        audit_log,
        memory_bank,
        agent_identity_uuid: str | None = None,
        classifier: TriageClassifierClient,
        goal_context: GoalContextProvider,
        owners_context: AccountOwnersContextProvider,
        items_writer: TriagedItemWriter | None = None,
        task_drafter: TaskDrafter | None = None,
        project_resolver: ProjectResolver | None = None,
        inbox_project_id: str | None = None,
        sender_context: SenderContextProvider | None = None,
        prompt_version: str = "v1",
        model: str = DEFAULT_MODEL,
        dedup_window_hours: int = DEFAULT_DEDUP_WINDOW_HOURS,
    ) -> None:
        # Constructor invariant (ADR 0019): task_drafter requires the full
        # writer chain. If any of items_writer/project_resolver/inbox_project_id
        # is None while task_drafter is set, drafts would either silently
        # skip the BQ link-back or have nowhere to land on no-match.
        if task_drafter is not None:
            missing = [
                name
                for name, value in (
                    ("items_writer", items_writer),
                    ("project_resolver", project_resolver),
                    ("inbox_project_id", inbox_project_id),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "TriageAgent: task_drafter is set but these dependencies "
                    f"are None: {missing}. Wire all four together or none."
                )
        super().__init__(
            agent_id=agent_id,
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
            agent_identity_uuid=agent_identity_uuid,
        )
        self._classifier = classifier
        self._goal_context = goal_context
        self._owners_context = owners_context
        self._items_writer = items_writer
        self._task_drafter = task_drafter
        self._project_resolver = project_resolver
        self._inbox_project_id = inbox_project_id
        self._sender_context = sender_context
        self._prompt_version = prompt_version
        self._model = model
        self._dedup_window_hours = dedup_window_hours
        self._prompt_template = self._load_prompt(PROMPT_NAME, prompt_version)

    # ---------------------------------------------------------------- _run

    def _run(self, input: TriageInput) -> TriageOutput:
        prompt = self._render_prompt(input)
        signal_block = self._render_signal_block(input)
        raw = self._classifier.classify(prompt=prompt, signal_block=signal_block)
        output = self._parse(raw)

        # ADR 0026: input_hash dedup pre-check. If this signal was already
        # classified within the window, skip the writer chain entirely.
        # The classification still ran (the LLM call is sunk cost) so the
        # audit row has a real output to summarize; we just don't draft
        # another Airtable Task or write another BQ row.
        if self._items_writer is not None:
            existing_id = self._items_writer.find_recent_by_hash(
                compute_input_hash(input),
                window_minutes=self._dedup_window_hours * 60,
            )
            if existing_id is not None:
                log.info(
                    "triage.dedup_skip: %s",
                    json.dumps(
                        {
                            "event": "triage.dedup_skip",
                            "existing_item_id": existing_id,
                            "source": input.source.value,
                            "source_event_ref": input.source_event_ref,
                            "window_hours": self._dedup_window_hours,
                        }
                    ),
                )
                return _with_dedup_marker(output, existing_id)

        # Order: draft Airtable FIRST, then write BQ row carrying the
        # airtable_task_record_id. Avoids the streaming-buffer problem on
        # post-write updates (BQ blocks updates for ~90 min after insert).
        # If Airtable POST raises, the BQ row still gets written with
        # airtable_task_record_id=NULL — locks down the "BQ remains audit
        # trail" contract that test_triage_agent.py exercises.
        airtable_record_id, account_id = self._maybe_draft_airtable(input, output)

        if self._items_writer is not None:
            run_id = str(uuid.uuid4())
            self._items_writer.write(
                input=input,
                output=output,
                run_id=run_id,
                model=self._model,
                prompt_version=self._prompt_version,
                airtable_task_record_id=airtable_record_id,
                account_id=account_id,
            )
        return output

    def _maybe_draft_airtable(
        self, input: TriageInput, output: TriageOutput
    ) -> tuple[str | None, str | None]:
        """Resolve sender → project, draft an Airtable Task row.

        Returns (airtable_record_id, account_id). Both may be None when
        drafting is skipped (no drafter wired, low LLM confidence,
        action_type=do_now) OR when the Airtable POST fails (BQ row still
        written without the link — preserves audit trail). account_id is
        populated even when the draft is skipped, as long as the resolver
        finds a match.
        """
        if self._task_drafter is None:
            return None, None
        if output.action_type is ActionType.DO_NOW:
            return None, None
        if output.confidence < CONFIDENCE_THRESHOLD:
            return None, None

        assert self._project_resolver is not None  # invariant guarded in __init__
        assert self._inbox_project_id is not None
        resolved: ResolverResult | None = self._project_resolver.resolve(
            input.sender, source=input.source
        )

        project_id: str
        owner_user_id: str | None
        account_id: str | None
        if resolved is not None:
            project_id = resolved.project_record_id
            owner_user_id = resolved.owner_user_id
            account_id = resolved.account_id
        else:
            project_id = self._inbox_project_id
            owner_user_id = None
            account_id = None
            log.info(
                "triage.no_project_match: %s",
                json.dumps(
                    {
                        "event": "triage.no_project_match",
                        "source": input.source.value,
                        "sender": input.sender,
                        "source_event_ref": input.source_event_ref,
                        "inbox_project_id": self._inbox_project_id,
                    }
                ),
            )

        try:
            airtable_record_id = self._task_drafter.draft(
                input=input,
                output=output,
                project_record_id=project_id,
                owner_user_id=owner_user_id,
            )
            return airtable_record_id, account_id
        except TaskDraftWriteError:
            log.exception(
                "airtable draft failed; BQ row remains audit trail " "(source_event_ref=%s)",
                input.source_event_ref,
            )
            return None, account_id

    # ----------------------------------------------------------- prompt

    def _render_prompt(self, input: TriageInput) -> str:
        # The prompt template is jinja-ish but we use Python string templating
        # to avoid a dependency. Triple-brace placeholders render as plain text.
        template = self._prompt_template
        sender_context_block = ""
        if self._sender_context is not None and input.sender:
            sender_context_block = self._sender_context.text_block(input.sender)
        substitutions = {
            "goal_context": self._goal_context.text_block(),
            "account_owners_context": self._owners_context.text_block(),
            "sender_context": sender_context_block,
            "source": input.source.value,
            "source_url": input.source_url,
            "sender": input.sender,
            "subject": input.subject,
            "body": input.body,
        }
        # Manual substitution — `{{name}}` -> value. Avoids brace-parsing
        # surprises that Python's str.format hits on JSON examples in the prompt.
        for key, value in substitutions.items():
            template = template.replace("{{" + key + "}}", str(value))
        return template

    def _render_signal_block(self, input: TriageInput) -> str:
        # Companion structured form for the classifier. The prompt also
        # contains the signal in-line; this duplicates it in case the classifier
        # implementation prefers structured tool input over inline templating.
        return json.dumps(
            {
                "source": input.source.value,
                "source_url": input.source_url,
                "source_event_ref": input.source_event_ref,
                "sender": input.sender,
                "subject": input.subject,
                "body": input.body,
                "ingested_at": input.ingested_at.isoformat(),
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------- parse

    def _parse(self, raw: str) -> TriageOutput:
        cleaned = _strip_json_fences(raw).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise TriageJSONError(
                f"classifier returned non-JSON: {exc}; first 200 chars: {cleaned[:200]!r}"
            ) from exc

        try:
            return TriageOutput(
                actionable=bool(data["actionable"]),
                owner_type=OwnerType(data["owner_type"]),
                action_type=ActionType(data["action_type"]),
                severity=Severity(data["severity"]),
                confidence=float(data["confidence"]),
                reasoning=str(data["reasoning"]),
                positive_goal_achieving=(
                    PGAStrength(data["positive_goal_achieving"])
                    if data.get("positive_goal_achieving") is not None
                    else None
                ),
                owner_email=data.get("owner_email"),
                category=(Category(data["category"]) if data.get("category") is not None else None),
                task_or_project=(
                    TaskOrProject(data["task_or_project"])
                    if data.get("task_or_project") is not None
                    else None
                ),
            )
        except (KeyError, ValueError) as exc:
            raise TriageJSONError(f"classifier JSON missing/invalid field: {exc}") from exc

    # ------------------------------------------ audit-log summarization

    def _summarize_input(self, input: TriageInput) -> str | None:
        # No body in the audit summary — bodies can carry PII even after
        # HIPAA filtering. Subject + sender + first 200 chars is enough to
        # join back to the source via source_event_ref if we need to debug.
        return json.dumps(
            {
                "source": input.source.value,
                "source_event_ref": input.source_event_ref,
                "sender": input.sender,
                "subject": input.subject[:200],
            }
        )

    def _summarize_output(self, output: TriageOutput) -> str | None:
        # The full classification is non-PII by construction — owner_email
        # is the only address-shaped field, and it's already going to BQ.
        summary: dict[str, object] = {
            "actionable": output.actionable,
            "owner_type": output.owner_type.value,
            "action_type": output.action_type.value,
            "severity": output.severity.value,
            "confidence": output.confidence,
            "positive_goal_achieving": (
                output.positive_goal_achieving.value if output.positive_goal_achieving else None
            ),
        }
        if output.dedup_skipped:
            # ADR 0026: surface dedup decision in the audit row.
            summary["dedup_skipped"] = True
            summary["dedup_existing_item_id"] = output.dedup_existing_item_id
        return json.dumps(summary)


def _with_dedup_marker(output: TriageOutput, existing_item_id: str) -> TriageOutput:
    """Return a copy of ``output`` flagged as a dedup-skip (ADR 0026)."""
    from dataclasses import replace

    return replace(
        output,
        dedup_skipped=True,
        dedup_existing_item_id=existing_item_id,
    )


def _strip_json_fences(raw: str) -> str:
    """Remove ```json ... ``` fences if a model returned them despite the prompt."""
    s = raw.strip()
    if s.startswith("```"):
        # Drop the first line (``` or ```json) and the final ```.
        lines = s.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return s
