"""Cloud Run Job entrypoint for the CRM Auto-updater (ADR 0047).

One invocation per scheduled fire (06:15 PT daily). Process:

  1. Load HIPAA domain set from BQ (forward-defense; today empty).
  2. Load known accounts + known contact emails from BQ for extractor.
  3. Read most-recent successful run checkpoint from
     ``agent_outputs.crm_updater_runs``.
  4. List ``secondbrain``-labeled messages NOT yet labeled
     ``secondbrain-processed`` (Gmail q= filter does both).
  5. For each message: invoke the agent → if successful, apply
     ``secondbrain-processed`` label.
  6. Write a new checkpoint row with run stats.

Environment variables:

  - ``BRAIN_PROJECT_ID`` (required)
  - ``CRM_UPDATER_DWD_TARGET_PRINCIPAL`` (default
    ``asb-agent-triage-sa@<project>.iam.gserviceaccount.com``) — the
    DWD-grantable SA we impersonate.
  - ``CRM_UPDATER_DWD_SUBJECT`` (default ``owner@example.com``)
  - ``CRM_UPDATER_LABEL`` (default ``secondbrain``)
  - ``CRM_UPDATER_PROCESSED_LABEL`` (default ``secondbrain-processed``)
  - ``CRM_UPDATER_MAX_MESSAGES_PER_RUN`` (default 50)
  - ``CRM_UPDATER_INBOX_PROJECT_RECORD_ID`` (Airtable Projects record id
    used for "Triage Inbox" — same field as TB_TRIAGE_INBOX_PROJECT_ID)
  - ``AIRTABLE_OPS_BASE_ID`` (the Operations base id)
  - ``AIRTABLE_TASKS_WRITE_PAT_SECRET_ID`` (Secret Manager id of the PAT)
  - ``CRM_UPDATER_SA_EMAIL`` (default ``asb-crm-updater-sa@<project>.iam.gserviceaccount.com``)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime

log = logging.getLogger("agency_brain.agents.crm_updater.main")
logging.basicConfig(level=logging.INFO)


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val or ""


def main(argv: list[str] | None = None) -> int:
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.dwd import DWDServiceFactory
    from ...common.memory_bank import InMemoryMemoryBank
    from ..notes_ingestor.embedder import VertexEmbedder
    from .agent import CrmUpdaterAgent
    from .airtable_writer import AirtableCRMWriteClient, CrmDraftWriter
    from .dedup import RunCheckpointStore, new_run_id
    from .extractor import Extractor, ExtractorConfig
    from .gmail_client import (
        GMAIL_MODIFY_SCOPE,
        GMAIL_READONLY_SCOPE,
        GmailClient,
    )
    from .hipaa_filter import HipaaFilter, load_hipaa_domains_from_bq
    from .models import CrmUpdaterInput, RunCheckpoint
    from .notes_writer import CrmNotesWriter

    project_id = _env("BRAIN_PROJECT_ID", required=True)
    target_principal = _env(
        "CRM_UPDATER_DWD_TARGET_PRINCIPAL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )
    subject = _env("CRM_UPDATER_DWD_SUBJECT", "owner@example.com")
    label = _env("CRM_UPDATER_LABEL", "secondbrain")
    processed_label = _env("CRM_UPDATER_PROCESSED_LABEL", "secondbrain-processed")
    max_msgs = int(_env("CRM_UPDATER_MAX_MESSAGES_PER_RUN", "50"))
    inbox_project = _env("CRM_UPDATER_INBOX_PROJECT_RECORD_ID", required=True)
    ops_base = _env("AIRTABLE_OPS_BASE_ID", required=True)
    tasks_pat_secret = _env("AIRTABLE_TASKS_WRITE_PAT_SECRET_ID", required=True)
    sa_email = _env(
        "CRM_UPDATER_SA_EMAIL",
        f"asb-crm-updater-sa@{project_id}.iam.gserviceaccount.com",
    )

    bq_client = bigquery.Client(project=project_id)
    bq_query_adapter = _BQQueryAdapter(bq_client)
    bq_writer_adapter = _BQWriterAdapter(bq_client)

    # --- HIPAA domains ---
    hipaa_domains = load_hipaa_domains_from_bq(bq_query=bq_query_adapter, project_id=project_id)
    hipaa_filter = HipaaFilter(hipaa_domains=hipaa_domains)

    # --- Known accounts / contacts (extractor reference lists) ---
    known_account_names = _load_known_account_names(bq_client, project_id)
    known_contact_emails = _load_known_contact_emails(bq_client, project_id)

    # --- Gmail (DWD-impersonated) ---
    readonly_factory = DWDServiceFactory(
        target_principal=target_principal,
        scope=GMAIL_READONLY_SCOPE,
        api="gmail",
        api_version="v1",
    )
    modify_factory = DWDServiceFactory(
        target_principal=target_principal,
        scope=GMAIL_MODIFY_SCOPE,
        api="gmail",
        api_version="v1",
    )
    gmail_client = GmailClient(
        readonly_factory=readonly_factory,
        modify_factory=modify_factory,
        subject=subject,
    )

    # --- Airtable (PAT-authed) ---
    pat = _read_secret(tasks_pat_secret, project_id)
    airtable = AirtableCRMWriteClient(base_id=ops_base, pat=pat)

    # --- Extractor + Agent ---
    extractor = Extractor(ExtractorConfig(project_id=project_id))
    audit_log = AuditLogClient(project_id=project_id)

    def _writer_factory(message_id: str) -> CrmDraftWriter:
        return CrmDraftWriter(
            airtable=airtable,
            inbox_project_record_id=inbox_project,
            message_id=message_id,
        )

    # ADR 0049 — Gmail-into-corpus side-effect writer. Empty
    # CRM_UPDATER_DISABLE_NOTES_WRITE disables; default ON.
    notes_write_disabled = _env("CRM_UPDATER_DISABLE_NOTES_WRITE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if notes_write_disabled:
        notes_writer = None
        log.info("crm_updater.main.notes_write_disabled")
    else:
        vertex_location = _env("VERTEX_LOCATION", "us-central1")
        embedding_model = _env("EMBEDDING_MODEL", "text-embedding-005")
        embedder = VertexEmbedder(project_id=project_id, location=vertex_location)
        notes_writer = CrmNotesWriter(
            bq_rows=bq_writer_adapter,
            bq_query=bq_query_adapter,
            embedder=embedder,
            project_id=project_id,
            embedding_model=embedding_model,
        )

    agent = CrmUpdaterAgent(
        extractor=extractor,
        writer_factory=_writer_factory,
        hipaa_filter=hipaa_filter,
        sa_email=sa_email,
        audit_log=audit_log,
        memory_bank=InMemoryMemoryBank(),
        known_account_names=known_account_names,
        known_contact_emails=known_contact_emails,
        notes_writer=notes_writer,
    )

    # --- Run loop ---
    checkpoint_store = RunCheckpointStore(
        bq_query=bq_query_adapter,
        bq_writer=bq_writer_adapter,
        project_id=project_id,
    )
    run_id = new_run_id()
    started_at = datetime.now(UTC)

    list_result = gmail_client.list_labeled_messages(label=label, max_results=max_msgs)
    log.info(
        "crm_updater.main.list_done count=%d label=%s",
        len(list_result.messages),
        label,
    )

    processed = 0
    drafts_created = 0
    errors = 0
    history_id_after: str | None = None

    for ref in list_result.messages:
        try:
            full = gmail_client.get_message(message_id=ref.message_id)
            if full.history_id:
                history_id_after = full.history_id
        except Exception:
            log.exception("crm_updater.main.get_failed message_id=%s", ref.message_id)
            errors += 1
            continue

        try:
            output = agent.invoke(CrmUpdaterInput(message=full))
        except Exception:
            log.exception("crm_updater.main.invoke_failed message_id=%s", ref.message_id)
            errors += 1
            continue

        processed += 1
        if not output.skipped:
            drafts_created += (
                len(output.write_result.task_record_ids)
                + output.write_result.contact_updates_appended
                + output.write_result.account_updates_appended
            )
            try:
                gmail_client.apply_label(message_id=ref.message_id, label_name=processed_label)
            except Exception:
                log.exception(
                    "crm_updater.main.label_apply_failed message_id=%s",
                    ref.message_id,
                )
                errors += 1

    ended_at = datetime.now(UTC)
    checkpoint_store.write(
        RunCheckpoint(
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            history_id_after=history_id_after,
            messages_processed=processed,
            drafts_created=drafts_created,
            errors=errors,
            success=errors == 0,
        )
    )
    log.info(
        "crm_updater.main.done run_id=%s processed=%d drafts=%d errors=%d",
        run_id,
        processed,
        drafts_created,
        errors,
    )
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------- adapters


class _BQQueryAdapter:
    def __init__(self, client) -> None:
        self._client = client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery

        params = []
        for p in parameters or []:
            ptype = p["type"]
            if ptype.startswith("ARRAY_"):
                inner = ptype.removeprefix("ARRAY_")
                params.append(bigquery.ArrayQueryParameter(p["name"], inner, p["value"]))
            else:
                params.append(bigquery.ScalarQueryParameter(p["name"], ptype, p["value"]))
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(r) for r in self._client.query(sql, job_config=job_config).result()]


class _BQWriterAdapter:
    def __init__(self, client) -> None:
        self._client = client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._client.insert_rows_json(table_ref, rows) or []


# ---------------------------------------------------------------- helpers


def _load_known_account_names(bq_client, project_id: str) -> tuple[str, ...]:
    sql = f"SELECT DISTINCT company_name FROM `{project_id}.airtable_replica.accounts` WHERE IFNULL(hipaa, FALSE) = FALSE AND company_name IS NOT NULL"  # noqa: S608, E501
    try:
        rows = bq_client.query(sql).result()
        return tuple(str(r["company_name"]) for r in rows if r.get("company_name"))
    except Exception:
        log.exception("crm_updater.main.account_load_failed")
        return ()


def _load_known_contact_emails(bq_client, project_id: str) -> tuple[str, ...]:
    sql = f"SELECT DISTINCT LOWER(email) AS email FROM `{project_id}.airtable_replica.contacts` WHERE email IS NOT NULL"  # noqa: S608, E501
    try:
        rows = bq_client.query(sql).result()
        return tuple(str(r["email"]) for r in rows if r.get("email"))
    except Exception:
        log.exception("crm_updater.main.contact_load_failed")
        return ()


def _read_secret(secret_id: str, project_id: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
