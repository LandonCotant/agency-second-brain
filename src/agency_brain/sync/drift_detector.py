"""Detect new Airtable columns and surface them to ``asb-schema-drift-alerts``.

PRD §6.2 forbids auto-applying schema drift. The detector compares the field
names a sync run actually saw on the wire against the columns codified in
``airtable/schema.json`` and publishes one Pub/Sub message per newly
discovered column. the operator reviews the alert and decides whether to update
``schema.json`` (and the matching replica DDL) in a follow-up PR.

PR-2 scope is **new columns only**. Type changes and removed columns are out
of scope per the design choice recorded in
``docs/adr/0010-cloud-run-job-for-airtable-sync.md``; revisit if either turns
out to matter operationally.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class DriftEvent:
    """One new-column detection. JSON-serialized for Pub/Sub."""

    table_name: str
    new_columns: tuple[str, ...]
    detected_at: datetime

    def to_message_bytes(self) -> bytes:
        body = {
            "table": self.table_name,
            "new_columns": list(self.new_columns),
            "detected_at": self.detected_at.astimezone(UTC).isoformat(),
            "recommended_action": (
                "Add the column to airtable/schema.json and the matching replica "
                "table DDL via a follow-up PR. Do NOT auto-apply (PRD §6.2)."
            ),
        }
        return json.dumps(body, sort_keys=True).encode("utf-8")


def detect_new_columns(
    table_name: str,
    observed_field_names: Iterable[str],
    known_field_names: Iterable[str],
) -> tuple[str, ...]:
    """Return columns present in the Airtable response but absent from the schema.

    Comparison is set-difference on the raw Airtable field names (with spaces),
    not the BQ slugs — drift surfaces use the column names a human will see in
    the Airtable UI when they go to investigate.
    """
    known = set(known_field_names)
    new = sorted(set(observed_field_names) - known)
    return tuple(new)


def publish_drift(
    publisher: Any,
    topic_path: str,
    event: DriftEvent,
) -> str:
    """Publish a drift event and return the Pub/Sub message ID.

    ``publisher`` is the ``google.cloud.pubsub_v1.PublisherClient`` (or a test
    double exposing the same ``publish`` interface). The message ID is
    returned so the orchestrator can log it for traceability.
    """
    future = publisher.publish(topic_path, event.to_message_bytes())
    return future.result(timeout=30)
