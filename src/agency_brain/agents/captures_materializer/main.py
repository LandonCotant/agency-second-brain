"""Cloud Run Job entrypoint for the Captures materializer (ADR 0039).

One execution per scheduler tick (every 15 min by default). Per tick:

  1. Read up to ``MAX_CAPTURES_PER_TICK`` unsynced rows from
     ``airtable_replica.captures``.
  2. For each row, dispatch by Kind (note/decision/win/todo), flip
     ``Synced`` in Airtable, then DELETE the source row.
  3. Emit one audit row per processed capture; log a tick summary.

Required env vars (set in
``terraform/modules/agent_runtime/captures_materializer.tf``):

  BRAIN_PROJECT_ID                   GCP project id
  AIRTABLE_BASE_ID                   Operations base (appXXXXXXXXXXXXXX)
  AIRTABLE_TASKS_WRITE_PAT_SECRET    Secret Manager secret name
                                     (default: airtable-tasks-write-pat-prod)
  CAPTURES_MATERIALIZER_SA_EMAIL     for the audit row's sa_email column
  MAX_CAPTURES_PER_TICK              bound (default 100)
  VERTEX_LOCATION                    default us-central1
  EMBEDDING_MODEL                    default text-embedding-005
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("agency_brain.agents.captures_materializer.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    airtable_base_id = os.environ["AIRTABLE_BASE_ID"]
    pat_secret_name = os.environ.get(
        "AIRTABLE_TASKS_WRITE_PAT_SECRET", "airtable-tasks-write-pat-prod"
    )
    sa_email = os.environ.get(
        "CAPTURES_MATERIALIZER_SA_EMAIL",
        f"asb-captures-materializer-sa@{project_id}.iam.gserviceaccount.com",
    )
    max_per_tick = int(os.environ.get("MAX_CAPTURES_PER_TICK", "100"))
    vertex_location = os.environ.get("VERTEX_LOCATION", "us-central1")
    embedding_model = os.environ.get("EMBEDDING_MODEL", "text-embedding-005")

    # Lazy imports — Cloud Run Jobs cold-start cost grows with import time.
    from google.cloud import bigquery, pubsub_v1

    from ...common.audit_log import AuditLogClient
    from ..notes_ingestor.embedder import VertexEmbedder
    from .agent import CapturesMaterializerAgent, run_tick
    from .airtable_writer import CapturesAirtableWriter, build_table_client
    from .dispatch import DispatchConfig
    from .readers import CapturesReader
    from .triage_publisher import topic_path

    bq_client = bigquery.Client(project=project_id)
    bq_rows = _BQRowsAdapter(bq_client)
    bq_query = _BQQueryAdapter(bq_client)
    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)

    pat = _read_secret(secret_id=pat_secret_name, project_id=project_id)
    table = build_table_client(base_id=airtable_base_id, pat=pat)
    airtable_writer = CapturesAirtableWriter(table=table)

    reader = CapturesReader(bq_query=bq_query, project_id=project_id)
    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
    )
    embedder = VertexEmbedder(project_id=project_id, location=vertex_location)

    config = DispatchConfig(
        project_id=project_id,
        notes_table_ref=f"{project_id}.agent_outputs.notes",
        decisions_table_ref=f"{project_id}.agent_outputs.decisions",
        wins_table_ref=f"{project_id}.agent_outputs.wins",
        triage_topic_path=topic_path(project_id=project_id),
        embedding_model=embedding_model,
    )

    agent = CapturesMaterializerAgent(
        sa_email=sa_email,
        audit_log=audit,
        airtable_writer=airtable_writer,
        config=config,
        bq=bq_rows,
        bq_query=bq_query,
        publisher=publisher,
        embedder=embedder,
    )

    captures = reader.read_unsynced(limit=max_per_tick)
    summary = run_tick(captures, agent=agent)

    log.info(
        json.dumps(
            {
                "event": "CAPTURES_MATERIALIZE_DONE",
                "listed": summary.listed,
                "materialized": summary.materialized,
                "triage_published": summary.triage_published,
                "deleted": summary.deleted,
                "failures": summary.failures,
            }
        )
    )
    return 0 if summary.failures == 0 else 1


# ---------------------------------------------------------------------- adapters


class _BQRowsAdapter:
    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._bq.insert_rows_json(table_ref, rows)


class _BQQueryAdapter:
    """Parameterized SELECT helper.

    Mirror of ``notes_ingestor.main._BQQueryAdapter`` so the readers +
    dispatch dedup pre-check can run without importing
    ``google.cloud.bigquery`` directly.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery

        params = [
            bigquery.ScalarQueryParameter(p["name"], p["type"], p["value"])
            for p in (parameters or [])
        ]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


def _read_secret(*, secret_id: str, project_id: str) -> str:
    """Mirror of ``triage.agent._read_secret`` — fetch latest version."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
