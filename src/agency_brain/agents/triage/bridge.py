"""Pub/Sub → in-process Triage classification (ADR 0061).

Cloud Run Job ``asb-triage-bridge`` runs every 5 minutes (Cloud Scheduler):

1. Pulls up to ``MAX_MESSAGES`` from ``asb-triage-input-sub``.
2. For each message, deserializes the JSON envelope to a :class:`TriageInput`
   and invokes a locally-built :class:`TriageAgent` (built via
   :func:`agents.triage.factory.build_triage_agent`). The agent classifies the
   signal with Gemini and owns the writer chain — it writes the row to
   ``agent_outputs.triaged_items`` and drafts the Airtable Task (ADR 0019).
3. On success / poison (malformed input, HIPAA guard trip): ack. On transient
   failure (Vertex / BQ / Airtable / malformed model output): leave un-acked —
   Pub/Sub retries (up to 5 attempts) then dead-letters to
   ``asb-triage-input-dlq`` per ``triage_pubsub.tf``.

ADR 0061 retired the Vertex AI Reasoning Engine. Classification used to run in
a managed RE that this bridge called over the network — a flat hourly
management fee for an always-hosted runtime serving a 5-minute cron. The bridge
now runs the same :class:`TriageAgent` in-process and scales to zero between
ticks. Prompt-version rollback moves from RE ``runtimeRevisions`` to container
image tags (ADR 0019), consistent with every other agent.

Env vars (set by the Cloud Run Job in ``triage_bridge.tf``):

- ``BRAIN_PROJECT_ID``
- ``TRIAGE_SUB_PATH`` — full subscription path ``projects/{p}/subscriptions/{s}``
- ``TRIAGE_SA_EMAIL`` — for the audit row's ``sa_email`` column
- ``AIRTABLE_OPS_BASE_ID`` / ``AIRTABLE_TASKS_WRITE_PAT_SECRET_ID`` /
  ``TB_TRIAGE_INBOX_PROJECT_ID`` — enable the Airtable writer leg
- ``MAX_MESSAGES`` — pull batch size, default 50
- ``PULL_TIMEOUT_S`` — pull deadline in seconds, default 30
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from ..base import HipaaGuardTripped
from .models import Source, TriageInput
from .triage_agent import TriageAgent

log = logging.getLogger("agency_brain.agents.triage.bridge")


# ---------------------------------------------------------------------------
# Per-message processing.
# ---------------------------------------------------------------------------


def process_message(
    *,
    raw_payload: bytes,
    agent: TriageAgent,
) -> tuple[bool, str]:
    """Process one Pub/Sub message body.

    Returns ``(should_ack, summary)``. ``False`` means "leave un-acked so
    Pub/Sub redelivers" (transient failure). ``True`` covers successful
    classification AND poison messages we want OUT of the queue (malformed
    JSON, missing required fields, HIPAA guard trip).

    ``BaseAgent.invoke`` emits the audit row on every path (success, run-time
    failure, HIPAA breach) before control returns here. The local
    :class:`TriageAgent` owns the dedup + Airtable + BQ writer chain, so a
    successful invoke has already drafted the Task and written the row.
    """
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        # No audit row — not enough input to attribute it to an invocation.
        # The Pub/Sub delivery counter tracks this; DLQ catches repeat offenders.
        log.warning("invalid_json: %s", exc)
        return True, "invalid_json"

    try:
        triage_input = _build_triage_input(payload)
    except (KeyError, ValueError) as exc:
        log.warning("invalid_input: %s", exc)
        return True, f"invalid_input: {exc}"

    try:
        agent.invoke(triage_input)
    except HipaaGuardTripped as exc:
        # Poison: a HIPAA-aspect signal trips on every redelivery, so retrying
        # is pointless. BaseAgent already emitted the breach audit row. Ack it
        # out of the queue. (Under the retired RE this surfaced as an error
        # envelope; now it's a clean exception from the local pre-flight.)
        log.warning("hipaa_guard_tripped: %s", exc)
        return True, "hipaa_guard_tripped"
    except Exception as exc:  # transient — BaseAgent emitted the failure audit
        log.warning("classification_failed: %s: %s", type(exc).__name__, exc)
        return False, f"failed: {type(exc).__name__}"

    return True, "classified"


# ---------------------------------------------------------------------------
# Pub/Sub pull loop.
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    sub_path = os.environ["TRIAGE_SUB_PATH"]
    sa_email = os.environ.get(
        "TRIAGE_SA_EMAIL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )
    max_messages = int(os.environ.get("MAX_MESSAGES", "50"))
    pull_timeout_s = int(os.environ.get("PULL_TIMEOUT_S", "30"))

    from google.cloud import pubsub_v1

    from .factory import build_triage_agent

    # The agent owns its BQ client, audit log, Vertex classifier, and the
    # Airtable writer chain (ADR 0061 folded this in from the retired RE).
    agent = build_triage_agent(project_id=project_id, sa_email=sa_email)
    subscriber = pubsub_v1.SubscriberClient()

    log.info(json.dumps({"event": "BRIDGE_START", "subscription": sub_path}))

    response = subscriber.pull(
        subscription=sub_path,
        max_messages=max_messages,
        timeout=pull_timeout_s,
    )

    if not response.received_messages:
        log.info(json.dumps({"event": "BRIDGE_IDLE", "pulled": 0}))
        return 0

    ack_ids: list[str] = []
    summaries: list[str] = []
    for received in response.received_messages:
        should_ack, summary = process_message(
            raw_payload=received.message.data,
            agent=agent,
        )
        summaries.append(summary)
        if should_ack:
            ack_ids.append(received.ack_id)

    if ack_ids:
        subscriber.acknowledge(subscription=sub_path, ack_ids=ack_ids)

    log.info(
        json.dumps(
            {
                "event": "BRIDGE_DONE",
                "pulled": len(response.received_messages),
                "acked": len(ack_ids),
                "outcomes": summaries,
            }
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _build_triage_input(payload: dict[str, Any]) -> TriageInput:
    """Translate the inbound JSON envelope into a TriageInput dataclass.

    Mirrors ``agent.py:_build_triage_input`` so a message published on the
    topic produces the same TriageInput regardless of which path consumes it.
    """
    return TriageInput(
        source=Source(payload["source"]),
        source_url=payload.get("source_url", ""),
        source_event_ref=payload.get("source_event_ref", ""),
        sender=payload["sender"],
        subject=payload["subject"],
        body=payload["body"],
        ingested_at=_parse_ts(payload.get("ingested_at")),
        aspects=list(payload.get("aspects", [])),
    )


def _parse_ts(raw: Any) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


if __name__ == "__main__":
    sys.exit(main())
