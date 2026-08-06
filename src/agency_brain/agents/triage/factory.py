"""Shared wiring for the local Triage Agent (ADR 0061).

:func:`build_triage_agent` constructs a fully-wired :class:`TriageAgent` —
Vertex classifier + goal/owner/sender context loaders + the Airtable writer
chain — from environment configuration.

History: this wiring previously lived in
``agents/triage/agent.py:TriageQueryAgent.set_up()`` to boot the Vertex AI
Reasoning Engine. ADR 0061 retires the RE and runs classification in-process
inside the ``asb-triage-bridge`` Cloud Run job, so the wiring moved here to be
shared by the bridge (and, until the RE is torn down, the deploy entrypoint).

The Airtable writer leg is gated all-or-nothing: when
``TB_TRIAGE_INBOX_PROJECT_ID`` / ``AIRTABLE_OPS_BASE_ID`` /
``AIRTABLE_TASKS_WRITE_PAT_SECRET_ID`` are all set, drafting is enabled;
otherwise the agent classifies (and writes the audit row) without drafting.
"""

from __future__ import annotations

import os
from typing import Any

from .goal_context import AccountOwnersContextLoader, GoalContextLoader
from .sender_context import SenderContactLoader
from .triage_agent import TriageAgent
from .vertex_classifier import VertexClassifier, VertexClassifierConfig

DEFAULT_PROJECT_ID = "agency-brain-demo"
DEFAULT_LOCATION = "us-central1"
DEFAULT_TRIAGE_SA = "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com"


def build_triage_agent(
    *,
    project_id: str | None = None,
    location: str | None = None,
    sa_email: str | None = None,
    bq_client: Any = None,
    audit_log: Any = None,
    memory_bank: Any = None,
) -> TriageAgent:
    """Build a fully-wired local :class:`TriageAgent` from env config.

    Optional args override the env/default resolution and let tests inject
    fakes. In production the bridge passes ``project_id``/``sa_email``
    explicitly and lets everything else default.
    """
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.memory_bank import InMemoryMemoryBank
    from ...sync.airtable_tasks_client import AirtableTasksWriteClient
    from .project_resolver import ProjectResolver
    from .writers import TaskDrafter, TriagedItemWriter

    project_id = project_id or os.environ.get("TB_PROJECT_ID", DEFAULT_PROJECT_ID)
    location = location or os.environ.get("TB_LOCATION", DEFAULT_LOCATION)
    sa_email = sa_email or os.environ.get("TB_TRIAGE_SA", DEFAULT_TRIAGE_SA)

    if bq_client is None:
        bq_client = bigquery.Client(project=project_id)
    if audit_log is None:
        audit_log = AuditLogClient(project_id=project_id, bq_client=bq_client)
    if memory_bank is None:
        memory_bank = InMemoryMemoryBank()

    # v0: Model Armor disabled at the call site (ADR 0017). The flag still
    # threads through so re-enabling is an env change, not a code change.
    enable_armor = os.environ.get("TB_ENABLE_MODEL_ARMOR", "false").lower() == "true"
    classifier = VertexClassifier(
        config=VertexClassifierConfig(
            project_id=project_id,
            location=location,
            enable_model_armor=enable_armor,
        ),
    )

    # Airtable writer chain (ADR 0019) — all four wired together or none.
    # Empty env values disable the Airtable leg (e.g. for local debug),
    # letting the agent classify-only without drafting.
    items_writer: TriagedItemWriter | None = None
    task_drafter: TaskDrafter | None = None
    project_resolver: ProjectResolver | None = None
    inbox_project_id = os.environ.get("TB_TRIAGE_INBOX_PROJECT_ID", "").strip()
    ops_base_id = os.environ.get("AIRTABLE_OPS_BASE_ID", "").strip()
    tasks_pat_secret_id = os.environ.get("AIRTABLE_TASKS_WRITE_PAT_SECRET_ID", "").strip()
    if inbox_project_id and ops_base_id and tasks_pat_secret_id:
        tasks_pat = _read_secret(tasks_pat_secret_id, project_id)
        airtable_tasks = AirtableTasksWriteClient(base_id=ops_base_id, pat=tasks_pat)
        task_drafter = TaskDrafter(airtable=airtable_tasks)
        project_resolver = ProjectResolver(
            bq_client=_BigQueryRowsClient(bq_client),
            project_id=project_id,
        )
        items_writer = TriagedItemWriter(
            bq_client=bq_client,
            project_id=project_id,
            dedup_client=_BigQueryDedupClient(bq_client),
        )

    # ADR 0026: dedup window is operator-tunable. Default 24h.
    dedup_window_hours = int(os.environ.get("TRIAGE_DEDUP_WINDOW_HOURS", "24"))

    return TriageAgent(
        sa_email=sa_email,
        audit_log=audit_log,
        memory_bank=memory_bank,
        agent_identity_uuid=os.environ.get("VERTEX_AI_AGENT_IDENTITY_UUID"),
        classifier=classifier,
        goal_context=GoalContextLoader(
            bq_client=_BigQueryRowsClient(bq_client),
            project_id=project_id,
        ),
        owners_context=AccountOwnersContextLoader(
            bq_client=_BigQueryRowsClient(bq_client),
            project_id=project_id,
        ),
        sender_context=SenderContactLoader(
            bq_client=_BigQueryRowsClient(bq_client),
            project_id=project_id,
        ),
        items_writer=items_writer,
        task_drafter=task_drafter,
        project_resolver=project_resolver,
        inbox_project_id=inbox_project_id or None,
        dedup_window_hours=dedup_window_hours,
    )


# --------------------------------------------------------- BQ adapters


class _BigQueryRowsClient:
    """Tiny adapter matching goal_context.BQQueryClient."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str) -> list[dict]:
        return [dict(row.items()) for row in self._bq.query(sql).result()]


class _BigQueryDedupClient:
    """Adapter implementing writers.BQDedupClient via a parameterized SELECT.

    ADR 0026: pre-INSERT dedup pre-check. SELECT against the streaming buffer
    is allowed (only DML hits the streaming-buffer wall — see ADR 0025), so
    this works on rows seconds old.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def find_recent_item_id_by_hash(
        self, table_ref: str, input_hash: str, window_minutes: int
    ) -> str | None:
        from google.cloud import bigquery

        # table_ref is operator-config (project.dataset.table); input_hash and
        # window_minutes are bound as query parameters.
        sql = (
            f"SELECT item_id FROM `{table_ref}` "  # noqa: S608
            "WHERE input_hash = @input_hash "
            "AND triaged_at >= TIMESTAMP_SUB("
            "CURRENT_TIMESTAMP(), INTERVAL @window_minutes MINUTE) "
            "ORDER BY triaged_at DESC LIMIT 1"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("input_hash", "STRING", input_hash),
                bigquery.ScalarQueryParameter("window_minutes", "INT64", window_minutes),
            ]
        )
        rows = list(self._bq.query(sql, job_config=job_config).result())
        if not rows:
            return None
        return rows[0]["item_id"]


def _read_secret(secret_id: str, project_id: str) -> str:
    """Fetch the latest version of a Secret Manager secret as a plain string."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")
