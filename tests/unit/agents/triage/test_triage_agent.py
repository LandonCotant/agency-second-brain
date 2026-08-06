"""Unit tests for `TriageAgent`. Mocks the LLM + context providers.

Real BQ/Airtable I/O lives in PR 2; real LLM call in PR 4. These tests
verify the core agent shape: prompt rendering, response parsing, audit
discipline (HIPAA, low-confidence routing, malformed JSON failure path).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from agency_brain.agents.base import HipaaGuardTripped
from agency_brain.agents.triage import TriageAgent
from agency_brain.agents.triage.models import (
    ActionType,
    OwnerType,
    PGAStrength,
    Severity,
    Source,
    TriageInput,
)
from agency_brain.agents.triage.triage_agent import TriageJSONError
from agency_brain.agents.triage.writers import TriagedItemWriter
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------- test doubles


class _StubClassifier:
    def __init__(self, json_response: str) -> None:
        self._json = json_response
        self.calls: list[dict] = []

    def classify(self, *, prompt: str, signal_block: str) -> str:
        self.calls.append({"prompt": prompt, "signal_block": signal_block})
        return self._json


class _StubGoalContext:
    def text_block(self) -> str:
        return (
            "G-2026Q2-01: Land 2 new e-commerce retainers (Quarterly)\n"
            "G-2026A-01: Reach $40k MRR by year end (1-year)\n"
        )


class _StubOwnersContext:
    def text_block(self) -> str:
        return "recAcc01 / Acme Corp / project recProj01 / owner owner@example.com\n"


class _RecordingBQ:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.rows.extend(rows)
        return []


# ----------------------------------------------------------------- helpers


def _load_fixture(name: str) -> TriageInput:
    raw = json.loads((FIXTURES / name).read_text())
    return TriageInput(
        source=Source(raw["source"]),
        source_url=raw["source_url"],
        source_event_ref=raw["source_event_ref"],
        sender=raw["sender"],
        subject=raw["subject"],
        body=raw["body"],
        ingested_at=datetime.fromisoformat(raw["ingested_at"]),
        aspects=raw["aspects"],
    )


def _make_agent(
    classifier_json: str,
    *,
    items_writer: TriagedItemWriter | None = None,
) -> tuple[TriageAgent, _RecordingBQ, _StubClassifier]:
    bq = _RecordingBQ()
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    classifier = _StubClassifier(classifier_json)
    agent = TriageAgent(
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid="agent-id-uuid-triage-test",
        classifier=classifier,
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        items_writer=items_writer,
    )
    return agent, bq, classifier


# ---------------------------------------------------------------- tests


def test_actionable_classification_returns_typed_output_and_emits_audit() -> None:
    agent, bq, classifier = _make_agent(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "strong",
                "owner_type": "brian",
                "owner_email": "owner@example.com",
                "action_type": "schedule",
                "category": "calls",
                "task_or_project": "task",
                "severity": "high",
                "confidence": 0.88,
                "reasoning": (
                    "Acme Corp (recAcc01) Q2 deliverable on goal G-2026Q2-01; the operator owns."
                ),
            }
        )
    )

    input_ = _load_fixture("sample_gmail_actionable.json")
    output = agent.invoke(input_)

    assert output.actionable is True
    assert output.positive_goal_achieving is PGAStrength.STRONG
    assert output.owner_type is OwnerType.BRIAN
    assert output.owner_email == "owner@example.com"
    assert output.action_type is ActionType.SCHEDULE
    assert output.severity is Severity.HIGH
    assert output.confidence == pytest.approx(0.88)

    # Prompt was rendered with goal + owners blocks injected.
    assert len(classifier.calls) == 1
    rendered = classifier.calls[0]["prompt"]
    assert "G-2026Q2-01" in rendered
    assert "recAcc01" in rendered
    assert "Q2 deliverable review" in rendered  # subject inlined into prompt

    # Audit row reflects success + classification, no body in summary.
    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row["agent_id"] == "triage"
    assert row["success"] is True
    assert row["hipaa_guard_status"] == "PASSED"
    assert row["human_review_routed"] is False
    assert row["confidence"] == pytest.approx(0.88)
    summary = json.loads(row["input_summary"])
    assert summary["sender"] == input_.sender
    assert summary["source_event_ref"] == input_.source_event_ref
    assert "body" not in summary  # discipline: PII never goes into audit input_summary


def test_non_actionable_with_null_pga_passes_validation() -> None:
    agent, bq, _ = _make_agent(
        json.dumps(
            {
                "actionable": False,
                "positive_goal_achieving": None,
                "owner_type": "na",
                "owner_email": None,
                "action_type": "defer",
                "category": None,
                "task_or_project": None,
                "severity": "info",
                "confidence": 0.95,
                "reasoning": "Industry newsletter; no active goal benefit within 60 days.",
            }
        )
    )

    out = agent.invoke(_load_fixture("sample_gmail_low_confidence.json"))

    assert out.actionable is False
    assert out.positive_goal_achieving is None
    assert out.severity is Severity.INFO
    assert bq.rows[0]["success"] is True


def test_hipaa_aspect_trips_guard_before_classifier_runs() -> None:
    agent, bq, classifier = _make_agent("never-called")
    with pytest.raises(HipaaGuardTripped):
        agent.invoke(_load_fixture("sample_hipaa_excluded.json"))

    # LLM was never invoked.
    assert classifier.calls == []
    # Audit row shows the trip.
    row = bq.rows[0]
    assert row["hipaa_guard_status"] == "TRIPPED"
    assert row["success"] is False
    assert "hipaa_excluded" in (row["error"] or "")


def test_low_confidence_output_routes_to_human_review() -> None:
    agent, bq, _ = _make_agent(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "weak",
                "owner_type": "delegate",
                "owner_email": "owner@example.com",
                "action_type": "defer",
                "category": "computer",
                "task_or_project": "task",
                "severity": "low",
                "confidence": 0.55,
                "reasoning": "Weak signal; classification uncertain.",
            }
        )
    )

    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    row = bq.rows[0]
    assert row["success"] is True
    assert row["human_review_routed"] is True
    assert row["confidence"] == pytest.approx(0.55)


def test_malformed_json_raises_triage_error_and_emits_failure_audit() -> None:
    agent, bq, _ = _make_agent("this is not json at all { broken")

    with pytest.raises(TriageJSONError):
        agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    row = bq.rows[0]
    assert row["success"] is False
    assert row["hipaa_guard_status"] == "PASSED"  # not a HIPAA failure
    assert row["error"] is not None
    assert "TriageJSONError" in row["error"]


def test_missing_required_field_raises_triage_error() -> None:
    agent, _, _ = _make_agent(json.dumps({"actionable": True, "confidence": 0.9}))
    with pytest.raises(TriageJSONError, match="missing/invalid field"):
        agent.invoke(_load_fixture("sample_gmail_actionable.json"))


def test_validation_rejects_actionable_with_null_pga() -> None:
    """The dataclass __post_init__ enforces spec §7.1+§7.2 invariants."""
    agent, _, _ = _make_agent(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": None,  # invalid: PGA required when actionable
                "owner_type": "brian",
                "owner_email": "owner@example.com",
                "action_type": "do_now",
                "category": "calls",
                "task_or_project": "task",
                "severity": "high",
                "confidence": 0.9,
                "reasoning": "...",
            }
        )
    )
    with pytest.raises(TriageJSONError, match="missing/invalid field"):
        agent.invoke(_load_fixture("sample_gmail_actionable.json"))


def test_writer_is_invoked_when_provided() -> None:
    """When `items_writer` is wired, _run() inserts to BQ after the classifier."""

    class _ItemsBQ:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[dict]]] = []

        def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
            self.calls.append((table_ref, rows))
            return []

    items_bq = _ItemsBQ()
    items_writer = TriagedItemWriter(bq_client=items_bq, project_id="agency-brain-demo")
    agent, audit_bq, _ = _make_agent(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "moderate",
                "owner_type": "delegate",
                "owner_email": "owner@example.com",
                "action_type": "delegate",
                "category": "computer",
                "task_or_project": "task",
                "severity": "medium",
                "confidence": 0.82,
                "reasoning": "Routine vendor question; delegate.",
            }
        ),
        items_writer=items_writer,
    )

    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    # Audit row written to the audit log table.
    assert len(audit_bq.rows) == 1
    # Triaged-item row written to triaged_items.
    assert len(items_bq.calls) == 1
    table_ref, rows = items_bq.calls[0]
    assert table_ref == "agency-brain-demo.agent_outputs.triaged_items"
    assert rows[0]["severity"] == "medium"
    assert rows[0]["actionable"] is True
    assert rows[0]["agent_run_id"]  # populated


def test_writer_is_skipped_when_not_provided() -> None:
    agent, bq, _ = _make_agent(
        json.dumps(
            {
                "actionable": False,
                "positive_goal_achieving": None,
                "owner_type": "na",
                "owner_email": None,
                "action_type": "defer",
                "category": None,
                "task_or_project": None,
                "severity": "info",
                "confidence": 0.95,
                "reasoning": "noise",
            }
        )
    )
    agent.invoke(_load_fixture("sample_gmail_low_confidence.json"))
    # Only the audit row.
    assert len(bq.rows) == 1


def test_strips_json_code_fences_if_model_returns_them() -> None:
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "actionable": False,
                "positive_goal_achieving": None,
                "owner_type": "na",
                "owner_email": None,
                "action_type": "defer",
                "category": None,
                "task_or_project": None,
                "severity": "info",
                "confidence": 1.0,
                "reasoning": "ok",
            }
        )
        + "\n```"
    )
    agent, _, _ = _make_agent(fenced)
    out = agent.invoke(_load_fixture("sample_gmail_low_confidence.json"))
    assert out.severity is Severity.INFO


# ============================================================================
# ADR 0019 — Resolver + TaskDrafter wire-in
# ============================================================================

from agency_brain.agents.triage.project_resolver import ResolverResult
from agency_brain.agents.triage.writers import TaskDrafter


class _StubResolver:
    """Returns a canned ResolverResult (or None for no-match)."""

    def __init__(self, result: ResolverResult | None) -> None:
        self._result = result
        self.calls: list[dict] = []

    def resolve(self, sender: str, *, source) -> ResolverResult | None:
        self.calls.append({"sender": sender, "source": source})
        return self._result


class _RecordingAirtable:
    """Implements writers.AirtableTasksClient — records and returns canned id."""

    def __init__(
        self, *, return_id: str = "recDraftedTask", raise_exc: Exception | None = None
    ) -> None:
        self._return_id = return_id
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    def create_task(self, fields: dict) -> str:
        self.calls.append(dict(fields))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_id


_INBOX_PROJECT_ID = "recProjTriageInbox"


def _make_drafting_agent(
    classifier_json: str,
    *,
    resolver_result: ResolverResult | None = None,
    airtable: _RecordingAirtable | None = None,
):
    """Build an agent with the full Airtable writer chain (ADR 0019)."""

    class _ItemsBQ:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[dict]]] = []

        def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
            self.calls.append((table_ref, rows))
            return []

    items_bq = _ItemsBQ()
    items_writer = TriagedItemWriter(bq_client=items_bq, project_id="agency-brain-demo")
    resolver = _StubResolver(resolver_result)
    if airtable is None:
        airtable = _RecordingAirtable()
    drafter = TaskDrafter(airtable=airtable)

    audit_bq = _RecordingBQ()
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=audit_bq)
    classifier = _StubClassifier(classifier_json)
    agent = TriageAgent(
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid="agent-id-uuid-triage-test",
        classifier=classifier,
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        items_writer=items_writer,
        task_drafter=drafter,
        project_resolver=resolver,
        inbox_project_id=_INBOX_PROJECT_ID,
    )
    return agent, items_bq, audit_bq, resolver, airtable


def _actionable_classifier_json(*, action_type: str = "schedule", confidence: float = 0.88) -> str:
    return json.dumps(
        {
            "actionable": True,
            "positive_goal_achieving": "moderate",
            "owner_type": "brian",
            "owner_email": "owner@example.com",
            "action_type": action_type,
            "category": "calls",
            "task_or_project": "task",
            "severity": "high",
            "confidence": confidence,
            "reasoning": "test",
        }
    )


def test_resolver_match_drafts_to_resolved_project_with_owner() -> None:
    resolved = ResolverResult(
        project_record_id="recProjAcme",
        owner_user_id="usrthe operator",
        account_id="recAcc01",
        match_reason="exact",
    )
    agent, items_bq, _, resolver, airtable = _make_drafting_agent(
        _actionable_classifier_json(), resolver_result=resolved
    )
    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    # Resolver was called with the sender + GMAIL source.
    assert len(resolver.calls) == 1
    assert resolver.calls[0]["source"] is Source.GMAIL

    # Airtable POST happened with the resolved project + owner.
    assert len(airtable.calls) == 1
    fields = airtable.calls[0]
    assert fields["Project"] == ["recProjAcme"]
    assert fields["Owner"] == "usrthe operator"
    assert fields["Source"] == "Triage Agent"
    assert fields["Approval Status"] == "Drafted by Agent"

    # BQ row carries the Airtable record id.
    _, rows = items_bq.calls[0]
    assert rows[0]["airtable_task_record_id"] == "recDraftedTask"


def test_resolver_no_match_drafts_to_inbox_project_and_logs() -> None:
    agent, items_bq, _, resolver, airtable = _make_drafting_agent(
        _actionable_classifier_json(), resolver_result=None
    )
    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    # Drafted to the sentinel Inbox.
    fields = airtable.calls[0]
    assert fields["Project"] == [_INBOX_PROJECT_ID]
    # No Owner set on no-match — resolver couldn't surface owner_user_id.
    assert "Owner" not in fields
    # BQ row still carries the inbox draft's record id.
    _, rows = items_bq.calls[0]
    assert rows[0]["airtable_task_record_id"] == "recDraftedTask"


def test_low_confidence_skips_airtable_draft_and_writes_bq_with_null_link() -> None:
    """LLM confidence < 0.7 → human-review path. No Airtable noise."""
    agent, items_bq, _, resolver, airtable = _make_drafting_agent(
        _actionable_classifier_json(confidence=0.55),
        resolver_result=ResolverResult(
            project_record_id="recProjAcme",
            owner_user_id="usrthe operator",
            account_id="recAcc01",
            match_reason="exact",
        ),
    )
    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    # Resolver never called (early return on confidence gate).
    assert resolver.calls == []
    # Airtable never called.
    assert airtable.calls == []
    # BQ row still written with NULL airtable link — preserves audit trail.
    _, rows = items_bq.calls[0]
    assert rows[0]["airtable_task_record_id"] is None
    assert rows[0]["human_review_routed"] is True


def test_do_now_action_type_skips_airtable_draft() -> None:
    """spec §5.1 — do_now actions don't get Airtable rows; the operator acts now."""
    agent, items_bq, _, resolver, airtable = _make_drafting_agent(
        _actionable_classifier_json(action_type="do_now", confidence=0.95),
        resolver_result=ResolverResult(
            project_record_id="recProjAcme",
            owner_user_id="usrthe operator",
            account_id="recAcc01",
            match_reason="exact",
        ),
    )
    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    assert resolver.calls == []
    assert airtable.calls == []
    _, rows = items_bq.calls[0]
    assert rows[0]["airtable_task_record_id"] is None
    assert rows[0]["action_type"] == "do_now"


def test_airtable_draft_failure_still_writes_bq_row() -> None:
    """Lock down the 'BQ remains audit trail' contract.

    If Airtable POST fails (rate limit, 5xx, malformed schema), the BQ row
    must STILL be written — it's the canonical record. The Airtable record
    id is just a foreign key that's NULL when the draft didn't land.
    """
    failing_airtable = _RecordingAirtable(raise_exc=RuntimeError("502 bad gateway"))
    agent, items_bq, audit_bq, _, _ = _make_drafting_agent(
        _actionable_classifier_json(),
        resolver_result=ResolverResult(
            project_record_id="recProjAcme",
            owner_user_id="usrthe operator",
            account_id="recAcc01",
            match_reason="exact",
        ),
        airtable=failing_airtable,
    )
    # Airtable POST was attempted and failed — but invoke() must succeed.
    agent.invoke(_load_fixture("sample_gmail_actionable.json"))

    # BQ row written with NULL airtable_task_record_id.
    assert len(items_bq.calls) == 1
    _, rows = items_bq.calls[0]
    assert rows[0]["airtable_task_record_id"] is None
    # Audit row reflects success — the agent's own invocation succeeded.
    assert len(audit_bq.rows) == 1
    assert audit_bq.rows[0]["success"] is True


def test_constructor_invariant_rejects_partial_writer_chain() -> None:
    """task_drafter without the full chain (items_writer + resolver + inbox)
    is a programmer error — drafts would either lose the BQ link-back or
    have nowhere to land on no-match."""

    class _NoOpAirtable:
        def create_task(self, fields: dict) -> str:
            return "recX"

    drafter = TaskDrafter(airtable=_NoOpAirtable())
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=_RecordingBQ())

    base_kwargs = dict(
        sa_email="x",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        classifier=_StubClassifier("{}"),
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        task_drafter=drafter,
    )

    # Missing all three downstream deps.
    with pytest.raises(ValueError, match="task_drafter is set but"):
        TriageAgent(**base_kwargs)

    # Missing only inbox_project_id.
    items_bq = _RecordingBQ()

    class _NoOpBQ:
        def query_rows(self, sql: str) -> list[dict]:
            return []

    from agency_brain.agents.triage.project_resolver import ProjectResolver

    resolver = ProjectResolver(bq_client=_NoOpBQ(), project_id="proj")
    with pytest.raises(ValueError, match="inbox_project_id"):
        TriageAgent(
            **base_kwargs,
            items_writer=TriagedItemWriter(bq_client=items_bq, project_id="proj"),
            project_resolver=resolver,
        )


# -------------------------------------------------------------- ADR 0026 dedup


class _StubDedupHit:
    """BQDedupClient that always reports a hit with the given item_id."""

    def __init__(self, existing_id: str) -> None:
        self._id = existing_id
        self.calls: list[tuple[str, str, int]] = []

    def find_recent_item_id_by_hash(
        self, table_ref: str, input_hash: str, window_minutes: int
    ) -> str | None:
        self.calls.append((table_ref, input_hash, window_minutes))
        return self._id


class _StubDedupMiss:
    """BQDedupClient that always reports a miss."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def find_recent_item_id_by_hash(
        self, table_ref: str, input_hash: str, window_minutes: int
    ) -> str | None:
        self.calls.append((table_ref, input_hash, window_minutes))
        return None


class _RecordingBQByTable:
    """BQ stub that captures rows per table_ref (ADR 0026 dedup tests).

    The simpler ``_RecordingBQ`` collapses everything into one list, which
    makes it impossible to assert "no triaged_items row written but the
    audit row WAS written".
    """

    def __init__(self) -> None:
        self.rows_by_table: dict[str, list[dict]] = {}

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.rows_by_table.setdefault(table_ref, []).extend(rows)
        return []


def test_dedup_hit_skips_writer_and_marks_output() -> None:
    bq = _RecordingBQByTable()
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    dedup = _StubDedupHit("existing-item-uuid-aaaa")
    items_writer = TriagedItemWriter(
        bq_client=bq, project_id="agency-brain-demo", dedup_client=dedup
    )
    classifier = _StubClassifier(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "strong",
                "owner_type": "brian",
                "owner_email": "owner@example.com",
                "action_type": "schedule",
                "category": None,
                "task_or_project": "task",
                "severity": "critical",
                "confidence": 0.95,
                "reasoning": "Same Client A signal as 1 hour ago.",
            }
        )
    )
    agent = TriageAgent(
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid="agent-id-uuid-triage-test",
        classifier=classifier,
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        items_writer=items_writer,
        dedup_window_hours=24,
    )

    input_ = _load_fixture("sample_gmail_actionable.json")
    output = agent.invoke(input_)

    # Output is flagged as a dedup-skip
    assert output.dedup_skipped is True
    assert output.dedup_existing_item_id == "existing-item-uuid-aaaa"

    # Dedup was checked exactly once with the configured window in minutes
    assert len(dedup.calls) == 1
    _table_ref, _hash, window_minutes = dedup.calls[0]
    assert window_minutes == 24 * 60

    # Critical: no triaged_items row was written
    items = bq.rows_by_table.get("agency-brain-demo.agent_outputs.triaged_items", [])
    assert items == []

    # The audit row from BaseAgent still fired and surfaces the dedup
    audit_rows = bq.rows_by_table.get("agency-brain-demo.agent_audit_log.events", [])
    assert len(audit_rows) == 1
    assert audit_rows[0]["success"] is True
    output_summary = json.loads(audit_rows[0]["output"])
    assert output_summary["dedup_skipped"] is True
    assert output_summary["dedup_existing_item_id"] == "existing-item-uuid-aaaa"


def test_dedup_miss_proceeds_with_normal_write() -> None:
    bq = _RecordingBQByTable()
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    dedup = _StubDedupMiss()
    items_writer = TriagedItemWriter(
        bq_client=bq, project_id="agency-brain-demo", dedup_client=dedup
    )
    classifier = _StubClassifier(
        json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "strong",
                "owner_type": "brian",
                "owner_email": "owner@example.com",
                "action_type": "schedule",
                "category": None,
                "task_or_project": "task",
                "severity": "critical",
                "confidence": 0.95,
                "reasoning": "Fresh signal, no recent dup.",
            }
        )
    )
    agent = TriageAgent(
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid="agent-id-uuid-triage-test",
        classifier=classifier,
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        items_writer=items_writer,
    )

    input_ = _load_fixture("sample_gmail_actionable.json")
    output = agent.invoke(input_)

    # Output not flagged as dedup
    assert output.dedup_skipped is False
    assert output.dedup_existing_item_id is None

    # Dedup was checked
    assert len(dedup.calls) == 1

    # And a triaged_items row WAS written (the normal path)
    items = bq.rows_by_table.get("agency-brain-demo.agent_outputs.triaged_items", [])
    assert len(items) == 1
