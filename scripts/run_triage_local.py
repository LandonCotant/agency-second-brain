"""Run the Triage Agent against a fixture file locally.

Default mode is `--dry-run` (no real writes). Override with `--write-bq`
to land a real row in `agent_outputs.triaged_items` (use sparingly during
manual verification of PR 4 deploys).

Usage:
    python scripts/run_triage_local.py path/to/fixture.json --dry-run
    python scripts/run_triage_local.py path/to/fixture.json --write-bq
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from agency_brain.agents.triage import TriageAgent
from agency_brain.agents.triage.models import Source, TriageInput
from agency_brain.agents.triage.writers import (
    TriagedItemWriter,
    build_triaged_item_row,
)
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank

PROJECT_ID = "agency-brain-demo"
SA_EMAIL = "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com"
MODEL = "gemini-2.5-flash"
PROMPT_VERSION = "v1"


class _FakeClassifier:
    """For local dry-runs without a real LLM call. Returns a fixed payload."""

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
                "reasoning": (
                    "Local dry-run stub classification — replace with real LLM via PR 4."
                ),
            }
        )


class _StubGoalContext:
    def text_block(self) -> str:
        return (
            "G-2026Q2-01: Land 2 e-commerce retainers (Quarterly)\n"
            "G-2026A-01: Reach $40k MRR by year end (1-year)\n"
        )


class _StubOwnersContext:
    def text_block(self) -> str:
        return "recCli01 / project recProj01 / owner owner@example.com\n"


class _DryRunBQ:
    """Records would-be inserts; never connects to BQ."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        for r in rows:
            print(f"\n[DRY-RUN] Would insert into {table_ref}:")
            print(json.dumps(r, indent=2))
        self.rows.extend(rows)
        return []


def _load_input(path: Path) -> TriageInput:
    raw = json.loads(path.read_text())
    return TriageInput(
        source=Source(raw["source"]),
        source_url=raw["source_url"],
        source_event_ref=raw["source_event_ref"],
        sender=raw["sender"],
        subject=raw["subject"],
        body=raw["body"],
        ingested_at=datetime.fromisoformat(raw["ingested_at"]),
        aspects=raw.get("aspects", []),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="Path to a TriageInput fixture JSON")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print would-be writes; never touch BQ. (default)",
    )
    parser.add_argument(
        "--write-bq",
        dest="dry_run",
        action="store_false",
        help="Write a real row to agent_outputs.triaged_items.",
    )
    args = parser.parse_args(argv)

    if not args.fixture.is_file():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    input_ = _load_input(args.fixture)
    audit_bq = _DryRunBQ()
    items_bq: object
    if args.dry_run:
        items_bq = _DryRunBQ()
    else:
        from google.cloud import bigquery  # type: ignore[import-not-found]

        items_bq = bigquery.Client(project=PROJECT_ID)

    items_writer = TriagedItemWriter(
        bq_client=items_bq,  # type: ignore[arg-type]
        project_id=PROJECT_ID,
    )
    audit = AuditLogClient(project_id=PROJECT_ID, bq_client=audit_bq)

    agent = TriageAgent(
        sa_email=SA_EMAIL,
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        agent_identity_uuid=str(uuid.uuid4()),
        classifier=_FakeClassifier(),
        goal_context=_StubGoalContext(),
        owners_context=_StubOwnersContext(),
        items_writer=items_writer,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
    )
    output = agent.invoke(input_)

    print("\n=== TriageOutput ===")
    print(f"actionable: {output.actionable}")
    print(f"severity: {output.severity.value}")
    print(f"confidence: {output.confidence}")
    print(f"reasoning: {output.reasoning}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
    else:
        print("\nrows landed in agent_outputs.triaged_items.")
    return 0


# Suppress "unused" warning on a helper kept for ad-hoc debugging.
_ = build_triaged_item_row


if __name__ == "__main__":
    sys.exit(main())
