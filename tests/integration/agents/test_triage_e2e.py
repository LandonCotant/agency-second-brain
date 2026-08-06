"""End-to-end Triage Agent test against real GCP services.

Skipped unless `INTEGRATION_GCP=1` in the environment. CI does not set
this; run locally only when verifying a deployment touchpoint.

Writes one row to `agent_outputs.triaged_items`. The row is recoverable
via `agent_run_id` printed at the end of the run.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION_GCP") != "1",
    reason="Set INTEGRATION_GCP=1 to run integration tests against agency-brain-demo",
)

from agency_brain.agents.triage import TriageAgent  # noqa: E402
from agency_brain.agents.triage.models import Source, TriageInput  # noqa: E402
from agency_brain.agents.triage.writers import TriagedItemWriter  # noqa: E402
from agency_brain.common.audit_log import AuditLogClient  # noqa: E402
from agency_brain.common.memory_bank import InMemoryMemoryBank  # noqa: E402


class _FixedClassifier:
    """Deterministic local response so the test doesn't depend on Vertex."""

    def classify(self, *, prompt: str, signal_block: str) -> str:
        return json.dumps(
            {
                "actionable": True,
                "positive_goal_achieving": "moderate",
                "owner_type": "brian",
                "owner_email": "owner@example.com",
                "action_type": "schedule",
                "category": "calls",
                "task_or_project": "task",
                "severity": "medium",
                "confidence": 0.82,
                "reasoning": "Integration smoke test — synthetic classification.",
            }
        )


class _StubCtx:
    def __init__(self, text: str) -> None:
        self._text = text

    def text_block(self) -> str:
        return self._text


def test_triage_writes_real_row_to_triaged_items() -> None:
    from google.cloud import bigquery  # type: ignore[import-not-found]

    project_id = "agency-brain-demo"
    bq = bigquery.Client(project=project_id)

    test_event_ref = f"integration-{uuid.uuid4().hex[:8]}"
    input_ = TriageInput(
        source=Source.GMAIL,
        source_url="https://example.invalid/integration-test",
        source_event_ref=test_event_ref,
        sender="integration-test@example.com",
        subject="Integration smoke test",
        body="This is a synthetic test message.",
        ingested_at=datetime.now(UTC),
        aspects=[],
    )

    items_writer = TriagedItemWriter(bq_client=bq, project_id=project_id)
    audit = AuditLogClient(project_id=project_id, bq_client=bq)
    agent = TriageAgent(
        sa_email="asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid=str(uuid.uuid4()),
        classifier=_FixedClassifier(),
        goal_context=_StubCtx("(test stub — no goals)"),
        owners_context=_StubCtx("(test stub — no owners)"),
        items_writer=items_writer,
    )
    output = agent.invoke(input_)
    assert output.actionable is True
    print(f"\nIntegration row source_event_ref: {test_event_ref}")
