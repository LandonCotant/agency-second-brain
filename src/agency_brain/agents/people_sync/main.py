"""Cloud Run Job entrypoint for People Sync (ADR 0057).

One Job execution = one sync tick (default Sunday 06:15 UTC, or manual
via ``gcloud run jobs execute`` / the ``sync_people`` MCP tool).

Per tick:
  1. Read accounts + contacts from ``airtable_replica`` (HIPAA cascade
     enforced in SQL).
  2. For each row: compose ``AccountNote`` / ``ContactNote`` (filename +
     frontmatter + body skeleton).
  3. Upsert against the corresponding ``Brain/05_GALAXY/{clients,
     people}/`` folder: create new files, update changed-frontmatter
     existing files, skip unchanged.
  4. Archive any Drive files whose ``airtable_id`` is no longer in the
     incoming row set (i.e., the Airtable row was deleted).
  5. Emit one ``run_summary`` audit row.

Required env vars:

  BRAIN_PROJECT_ID                  GCP project id
  BRAIN_GALAXY_ACCOUNTS_FOLDER_ID    Drive folder id of Brain/05_GALAXY/01_ACCOUNTS/
  BRAIN_GALAXY_CONTACTS_FOLDER_ID    Drive folder id of Brain/05_GALAXY/02_CONTACTS/
  PEOPLE_SYNC_SA_EMAIL              for the audit row's sa_email column
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid

log = logging.getLogger("agency_brain.agents.people_sync.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    accounts_folder_id = os.environ.get("BRAIN_GALAXY_ACCOUNTS_FOLDER_ID", "").strip()
    contacts_folder_id = os.environ.get("BRAIN_GALAXY_CONTACTS_FOLDER_ID", "").strip()
    sa_email = os.environ.get("PEOPLE_SYNC_SA_EMAIL") or (
        f"asb-people-sync-sa@{project_id}.iam.gserviceaccount.com"
    )

    if not accounts_folder_id and not contacts_folder_id:
        log.warning(
            "people_sync.skip: neither BRAIN_GALAXY_ACCOUNTS_FOLDER_ID nor BRAIN_GALAXY_CONTACTS_FOLDER_ID set"
        )
        return 0

    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    log.info(
        "people_sync.start run_id=%s project=%s clients_folder=%s people_folder=%s",
        run_id,
        project_id,
        accounts_folder_id,
        contacts_folder_id,
    )

    # Lazy imports — keep cold-start light.
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ..notes_ingestor.drive_client import ADCDriveServiceFactory
    from .airtable_reader import AirtableReader
    from .bq_enricher import AccountEnricher, ContactEnricher
    from .drive_writer import PeopleDriveWriter
    from .markdown_writer import make_account_note, make_contact_note
    from .models import SyncSummary
    from .writer import PeopleSyncAuditWriter

    bq_client = bigquery.Client(project=project_id)
    bq_adapter = _ParameterizedBQAdapter(bq_client)
    reader = AirtableReader(bq_query=bq_adapter, project_id=project_id)
    account_enricher = AccountEnricher(bq_query=bq_adapter, project_id=project_id)
    contact_enricher = ContactEnricher(bq_query=bq_adapter, project_id=project_id)

    drive_factory = ADCDriveServiceFactory()
    accounts_writer = (
        PeopleDriveWriter(service_factory=drive_factory, parent_folder_id=accounts_folder_id)
        if accounts_folder_id
        else None
    )
    contacts_writer = (
        PeopleDriveWriter(service_factory=drive_factory, parent_folder_id=contacts_folder_id)
        if contacts_folder_id
        else None
    )

    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)
    audit_writer = PeopleSyncAuditWriter(audit=audit, sa_email=sa_email)

    summary = SyncSummary()
    error: str | None = None

    try:
        if accounts_writer is not None:
            summary = _sync_accounts(
                reader=reader,
                writer=accounts_writer,
                summary=summary,
                make_note=make_account_note,
                enricher=account_enricher,
            )
        if contacts_writer is not None:
            summary = _sync_contacts(
                reader=reader,
                writer=contacts_writer,
                summary=summary,
                make_note=make_contact_note,
                enricher=contact_enricher,
            )
    except Exception as exc:
        log.exception("people_sync.fatal")
        error = f"{type(exc).__name__}: {exc}"

    total_latency_ms = int((time.perf_counter() - started) * 1000)
    audit_writer.emit_run_summary(
        run_id=run_id, summary=summary, latency_ms=total_latency_ms, error=error
    )
    log.info(
        "people_sync.done run_id=%s "
        "accounts=created:%d/updated:%d/unchanged:%d/archived:%d/failed:%d "
        "contacts=created:%d/updated:%d/unchanged:%d/archived:%d/failed:%d "
        "latency_ms=%d",
        run_id,
        summary.accounts_created,
        summary.accounts_updated,
        summary.accounts_unchanged,
        summary.accounts_archived,
        summary.accounts_failed,
        summary.contacts_created,
        summary.contacts_updated,
        summary.contacts_unchanged,
        summary.contacts_archived,
        summary.contacts_failed,
        total_latency_ms,
    )
    if error is not None:
        return 1
    return 0 if (summary.accounts_failed == 0 and summary.contacts_failed == 0) else 1


# ---------------------------------------------------------------------------
# per-table sync routines
# ---------------------------------------------------------------------------


def _sync_accounts(*, reader, writer, summary, make_note, enricher=None):
    from dataclasses import replace

    rows = reader.list_accounts()
    s = replace(summary, accounts_listed=len(rows))

    existing = writer.list_existing()  # {airtable_id → ExistingFile}
    incoming_ids = {r.airtable_id for r in rows}

    for row in rows:
        note = make_note(row)
        auto_sections = None
        if enricher is not None:
            auto_sections = {
                "## Active engagements": enricher.active_engagements(airtable_id=row.airtable_id),
                "## Recent activity": enricher.recent_activity(
                    account_name=row.name, airtable_id=row.airtable_id
                ),
                "## Open risks": enricher.open_risks(airtable_id=row.airtable_id),
            }
        outcome = writer.upsert(
            filename=note.filename,
            airtable_id=row.airtable_id,
            new_frontmatter=note.frontmatter,
            body_skeleton=note.body_skeleton,
            auto_sections=auto_sections,
        )
        if outcome.error is not None:
            s = replace(s, accounts_failed=s.accounts_failed + 1)
        elif outcome.created:
            s = replace(s, accounts_created=s.accounts_created + 1)
        elif outcome.frontmatter_updated:
            s = replace(s, accounts_updated=s.accounts_updated + 1)
        elif outcome.skipped_unchanged:
            s = replace(s, accounts_unchanged=s.accounts_unchanged + 1)

    # Archive rows that vanished from Airtable.
    for airtable_id in existing.keys() - incoming_ids:
        archived = writer.mark_archived(airtable_id=airtable_id)
        if archived is None or archived.error is not None:
            if archived is not None and archived.error is not None:
                s = replace(s, accounts_failed=s.accounts_failed + 1)
            continue
        if archived.frontmatter_updated:
            s = replace(s, accounts_archived=s.accounts_archived + 1)
    return s


def _sync_contacts(*, reader, writer, summary, make_note, enricher=None):
    from dataclasses import replace

    rows = reader.list_contacts()
    s = replace(summary, contacts_listed=len(rows))

    existing = writer.list_existing()
    incoming_ids = {r.airtable_id for r in rows}

    for row in rows:
        note = make_note(row)
        auto_sections = None
        if enricher is not None:
            auto_sections = {
                "## Conversation log": enricher.conversation_log(
                    contact_name=row.name,
                    email=row.email,
                ),
            }
        outcome = writer.upsert(
            filename=note.filename,
            airtable_id=row.airtable_id,
            new_frontmatter=note.frontmatter,
            body_skeleton=note.body_skeleton,
            auto_sections=auto_sections,
        )
        if outcome.error is not None:
            s = replace(s, contacts_failed=s.contacts_failed + 1)
        elif outcome.created:
            s = replace(s, contacts_created=s.contacts_created + 1)
        elif outcome.frontmatter_updated:
            s = replace(s, contacts_updated=s.contacts_updated + 1)
        elif outcome.skipped_unchanged:
            s = replace(s, contacts_unchanged=s.contacts_unchanged + 1)

    for airtable_id in existing.keys() - incoming_ids:
        archived = writer.mark_archived(airtable_id=airtable_id)
        if archived is None:
            continue
        if archived.error is not None:
            s = replace(s, contacts_failed=s.contacts_failed + 1)
        elif archived.frontmatter_updated:
            s = replace(s, contacts_archived=s.contacts_archived + 1)
    return s


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


class _ParameterizedBQAdapter:
    """BQQueryClient adapter — mirrors evening_reflection.main pattern.

    Currently no parameters are needed (queries are static), but the
    signature stays parameter-aware for future extensibility.
    """

    def __init__(self, bq_client) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery as bq_module

        params = []
        for p in parameters or []:
            ptype = p.get("type", "STRING")
            params.append(bq_module.ScalarQueryParameter(p["name"], ptype, p["value"]))
        job_config = bq_module.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


if __name__ == "__main__":
    sys.exit(main())
