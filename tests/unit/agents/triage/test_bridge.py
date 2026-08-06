"""Unit tests for the Pub/Sub → in-process Triage bridge (ADR 0061).

The bridge no longer calls a Reasoning Engine — it builds a local
:class:`TriageAgent` (via ``factory.build_triage_agent`` in production) and
invokes it per message. These tests construct a classify-only ``TriageAgent``
(no Airtable/BQ writer chain) with a fake classifier and exercise the bridge's
ack/nack contract:

- happy path: classify, audit row emitted via BaseAgent.
- HIPAA pre-flight: aspect=["hipaa_excluded"] trips before the classifier is
  called; audit row TRIPPED; ``process_message`` acks it (poison).
- malformed model output: ``TriageJSONError`` → failure audit → nack.
- ``process_message`` poison handling: malformed JSON / missing required
  fields ack without an audit row (no agent invocation).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agency_brain.agents.base import HipaaGuardTripped
from agency_brain.agents.triage.bridge import process_message
from agency_brain.agents.triage.models import (
    ActionType,
    PGAStrength,
    Severity,
    Source,
    TriageInput,
)
from agency_brain.agents.triage.triage_agent import TriageAgent
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank

# --------------------------------------------------------------- test doubles


class _RecordingBQ:
    """Captures both audit-log rows and triaged_items rows."""

    def __init__(self) -> None:
        self.rows_by_table: dict[str, list[dict]] = {}

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.rows_by_table.setdefault(table_ref, []).extend(rows)
        return []


class _FakeClassifier:
    """Stand-in for VertexClassifier. Returns the JSON the model would produce,
    or raises to simulate a transient Vertex failure."""

    def __init__(self, result: dict | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, str]] = []

    def classify(self, *, prompt: str, signal_block: str) -> str:
        self.calls.append({"prompt": prompt, "signal_block": signal_block})
        if isinstance(self._result, Exception):
            raise self._result
        return json.dumps(self._result)


class _FakeContext:
    """Goal/owners/sender context loader stub. text_block() ignores args."""

    def text_block(self, *args: object) -> str:
        return ""


# ------------------------------------------------------------------ helpers


_SA = "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com"
_GOOD_INPUT = TriageInput(
    source=Source.GMAIL,
    source_url="https://mail.google.com/x",
    source_event_ref="msg-001",
    sender="acme@example.com",
    subject="Need website update",
    body="Can you turn around copy changes by Friday?",
    ingested_at=datetime(2026, 4, 29, 16, 0, tzinfo=UTC),
    aspects=[],
)
_GOOD_RESULT = {
    "actionable": True,
    "owner_type": "delegate",
    "action_type": "delegate",
    "severity": "medium",
    "confidence": 0.85,
    "reasoning": "client request, mid-priority",
    "positive_goal_achieving": "moderate",
    "owner_email": "delegate@example.com",
    "category": None,
    "task_or_project": "task",
}
_GOOD_PAYLOAD = {
    "source": "gmail",
    "source_url": "https://mail.google.com/x",
    "source_event_ref": "msg-001",
    "sender": "acme@example.com",
    "subject": "Need website update",
    "body": "Can you turn around copy changes by Friday?",
    "ingested_at": "2026-04-29T16:00:00+00:00",
    "aspects": [],
}


def _make_agent(*, classifier_result: dict | Exception, bq: _RecordingBQ) -> TriageAgent:
    """Build a classify-only TriageAgent (no Airtable/BQ writer chain) — the
    same agent the factory builds in production, minus the writer leg, so these
    tests stay focused on the bridge's ack/nack contract."""
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=bq)
    return TriageAgent(
        sa_email=_SA,
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        classifier=_FakeClassifier(classifier_result),
        goal_context=_FakeContext(),
        owners_context=_FakeContext(),
        sender_context=_FakeContext(),
    )


def _audit_rows(bq: _RecordingBQ) -> list[dict]:
    return bq.rows_by_table.get("agency-brain-demo.agent_audit_log.events", [])


def _triaged_items_rows(bq: _RecordingBQ) -> list[dict]:
    return bq.rows_by_table.get("agency-brain-demo.agent_outputs.triaged_items", [])


# ================================================================ local TriageAgent


def test_local_agent_happy_path_classifies_and_audits():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    output = agent.invoke(_GOOD_INPUT)

    assert output.action_type is ActionType.DELEGATE
    assert output.severity is Severity.MEDIUM
    assert output.confidence == pytest.approx(0.85)
    assert output.positive_goal_achieving is PGAStrength.MODERATE

    # Classify-only build → no triaged_items write here (the writer leg is
    # wired in production via the factory; covered by test_triage_agent.py).
    assert _triaged_items_rows(bq) == []

    # One audit row (BaseAgent contract). agent_id is "triage" now — the local
    # agent, not the retired "triage-bridge" RE wrapper.
    audit = _audit_rows(bq)
    assert len(audit) == 1
    assert audit[0]["agent_id"] == "triage"
    assert audit[0]["sa_email"] == _SA
    assert audit[0]["success"] is True
    assert audit[0]["hipaa_guard_status"] == "PASSED"
    assert audit[0]["human_review_routed"] is False


def test_local_agent_hipaa_aspect_trips_before_classify():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    hipaa_input = TriageInput(
        source=Source.GMAIL,
        source_url="",
        source_event_ref="msg-002",
        sender="hospice@example.com",
        subject="Patient Smith intake",
        body="...",
        ingested_at=datetime(2026, 4, 29, 16, 0, tzinfo=UTC),
        aspects=["hipaa_excluded"],
    )

    with pytest.raises(HipaaGuardTripped):
        agent.invoke(hipaa_input)

    assert _triaged_items_rows(bq) == []
    audit = _audit_rows(bq)
    assert len(audit) == 1
    assert audit[0]["hipaa_guard_status"] == "TRIPPED"
    assert audit[0]["success"] is False


def test_local_agent_malformed_model_output_raises_and_audits_failure():
    bq = _RecordingBQ()
    bad = dict(_GOOD_RESULT)
    bad["severity"] = "not_a_real_severity"
    agent = _make_agent(classifier_result=bad, bq=bq)

    with pytest.raises(Exception):
        agent.invoke(_GOOD_INPUT)

    assert _triaged_items_rows(bq) == []
    audit = _audit_rows(bq)
    assert len(audit) == 1
    assert audit[0]["success"] is False


def test_local_agent_low_confidence_marks_audit_human_review_routed():
    bq = _RecordingBQ()
    low = dict(_GOOD_RESULT)
    low["confidence"] = 0.5
    agent = _make_agent(classifier_result=low, bq=bq)

    agent.invoke(_GOOD_INPUT)

    audit = _audit_rows(bq)
    assert audit[0]["human_review_routed"] is True


# ================================================================ process_message


def test_process_message_acks_on_success():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    should_ack, summary = process_message(
        raw_payload=json.dumps(_GOOD_PAYLOAD).encode("utf-8"),
        agent=agent,
    )

    assert should_ack is True
    assert summary == "classified"
    assert _audit_rows(bq)[0]["success"] is True


def test_process_message_acks_on_invalid_json_without_audit():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    should_ack, summary = process_message(raw_payload=b"not json at all", agent=agent)

    assert should_ack is True  # poison — get it out of the queue
    assert summary == "invalid_json"
    assert _audit_rows(bq) == []  # no agent invocation, no audit row


def test_process_message_acks_on_missing_required_field():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    payload = {
        "source": "gmail",
        # missing sender, subject, body
        "source_url": "",
        "source_event_ref": "",
    }

    should_ack, summary = process_message(
        raw_payload=json.dumps(payload).encode("utf-8"),
        agent=agent,
    )

    assert should_ack is True  # poison
    assert summary.startswith("invalid_input")


def test_process_message_nacks_on_classification_failure():
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=RuntimeError("vertex down"), bq=bq)

    should_ack, summary = process_message(
        raw_payload=json.dumps(_GOOD_PAYLOAD).encode("utf-8"),
        agent=agent,
    )

    assert should_ack is False  # transient — let Pub/Sub redeliver → DLQ
    assert summary.startswith("failed:")
    # BaseAgent emitted a failure audit row before re-raising.
    assert _audit_rows(bq)[0]["success"] is False


def test_process_message_acks_on_hipaa_guard_tripped():
    """A HIPAA-aspect signal trips on every redelivery, so ack it out."""
    bq = _RecordingBQ()
    agent = _make_agent(classifier_result=_GOOD_RESULT, bq=bq)

    payload = dict(_GOOD_PAYLOAD, aspects=["hipaa_excluded"])

    should_ack, summary = process_message(
        raw_payload=json.dumps(payload).encode("utf-8"),
        agent=agent,
    )

    assert should_ack is True
    assert summary == "hipaa_guard_tripped"
    assert _audit_rows(bq)[0]["hipaa_guard_status"] == "TRIPPED"
