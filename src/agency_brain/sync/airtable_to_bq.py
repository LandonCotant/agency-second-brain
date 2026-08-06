"""Airtable → BigQuery sync orchestrator (PRD §6.2).

Cloud Scheduler invokes the Cloud Run Job ``asb-airtable-sync`` every 15
minutes; the job runs as ``asb-sync-airtable-sa`` (custom IAM role,
PRD §4.2 / §4.8) and calls :func:`sync` here.

Per-table flow:

1. Build the ``filterByFormula`` (HIPAA Lookup-based, PRD §4.1 layer 2).
2. Pull every matching record from Airtable in a single pass.
3. Detect new columns; publish one Pub/Sub message per drift event to
   ``asb-schema-drift-alerts`` (never auto-apply, PRD §6.2).
4. Translate Airtable records into the BQ row layout from
   :mod:`schema_mapping` and replace the replica table contents via a
   ``WRITE_TRUNCATE`` load job. Atomic from BigQuery's perspective.
5. Update the matching ``_sync_checkpoints`` row.

Why ``WRITE_TRUNCATE`` over ``MERGE``:

The acceptance contract is "running sync twice produces the same end state"
and "flipping ``HIPAA = true`` removes the row within one cycle." Both fall
out of full-snapshot replacement for free. Replica tables are small
(low-hundreds of rows), so the cost difference vs. an incremental MERGE is
noise. ADR 0010 records the design choice.

The orchestrator is wired through ``main()`` from environment variables so
the same module is callable from tests with injected fakes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .airtable_client import AirtableClient
from .drift_detector import DriftEvent, detect_new_columns, publish_drift
from .hipaa_filters import hipaa_filter_for
from .schema_mapping import (
    BqField,
    replica_table_schemas,
    slugify_field_name,
    slugify_table_name,
)

if TYPE_CHECKING:
    from google.cloud import bigquery


log = logging.getLogger("agency_brain.sync.airtable_to_bq")

REPLICA_DATASET_DEFAULT = "airtable_replica"
CHECKPOINTS_TABLE = "_sync_checkpoints"

# Tables to sync, in dependency order: parents first so a downstream consumer
# reading mid-run sees consistent foreign keys when possible. Each entry is
# the Airtable table name as it appears in ``schema.json``. Single-base after
# ADR 0020 — Accounts/Contacts/Contracts replace the previous Clients table
# and the legacy CRM base.
SYNC_TABLES_ORDER: tuple[str, ...] = (
    # Configuration tables first — no inbound links from operational tables.
    "Service Catalog",
    "Team",
    "Risk Profiles",
    # Accounts before everything that links to them (Contacts, Contracts, Projects).
    "Accounts",
    # Contacts + Contracts before Projects so reverse-links are present mid-run.
    "Contacts",
    "Contracts",
    # Goals before Projects/Tasks (their Goal Linked → Goals; Goals.Parent Goal
    # is self-referential and tolerated mid-sync).
    "Goals",
    "Projects",
    "Tasks",
    "Goal Scores",
    # ADR 0039 — quick-capture form target. No inbound links from any other
    # table (the materializer reads it; nothing else does), so order at the
    # end is fine.
    "Captures",
    # Audit F7 (2026-05-28). Links to Projects + Goals + Team (Requester),
    # so sync after all three. Operator-authored; no inbound from any agent.
    "Orchestrator Inbox",
)

# Used ONLY to populate the ``_airtable_last_modified`` system column.
# Do NOT wire this into the pull filter: the load is WRITE_TRUNCATE, so an
# ``IS_AFTER(checkpoint)`` query filter would truncate the replica down to
# just the delta (and incremental MERGE can't propagate Airtable deletions
# anyway). Full pulls everywhere is the design — small tables, atomic
# replace. ADR 0010 records the incremental deferral; the half-wired
# checkpoint filter was removed 2026-06-10 after the code review caught
# that only a no-op MERGE was keeping it inert.
LAST_MODIFIED_FIELD_BY_TABLE: dict[str, str] = {
    "Accounts": "Last Activity Timestamp",
    "Projects": "Last Modified",
    "Tasks": "Last Modified",
}


@dataclass
class SyncResult:
    """Per-table summary written to the structured log on completion."""

    table_name: str
    rows_synced: int = 0
    invalid_rows: int = 0
    drift_columns: tuple[str, ...] = ()
    drift_message_ids: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str | None = None


@dataclass
class SyncRunSummary:
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    tables: list[SyncResult] = field(default_factory=list)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "tables": [dataclasses.asdict(t) for t in self.tables],
        }


# ---------------------------------------------------------------------------
# Record translation
# ---------------------------------------------------------------------------


def _translate_value(field_def: dict[str, Any], raw_value: Any) -> Any:
    """Translate an Airtable field value into the BQ-loadable shape.

    Most Airtable types are pass-through: dates are ISO strings, checkboxes
    are bools, multipleSelects are lists, multipleRecordLinks are lists of
    record IDs — exactly what BigQuery's load job wants.

    Collaborator types are the exception. Airtable returns each user as an
    object ``{"id": "usrXXX", "email": "...", "name": "..."}``. By default
    we extract ``email`` so the BQ value is a STRING that joins on
    ``Team.workspace_email``.

    A field def may opt into extracting ``user_id`` instead via the
    ``_extract: "user_id"`` annotation (ADR 0019). ``Team.User`` uses this
    so the Brain can populate ``Tasks.Owner`` with an Airtable ``usrXXX``
    when drafting. Default behavior (extract email) is unchanged for every
    other singleCollaborator field.
    """
    field_type = field_def.get("type")
    if raw_value is None:
        return None
    if field_type == "singleCollaborator":
        extract = field_def.get("_extract", "email")
        if isinstance(raw_value, dict):
            if extract == "user_id":
                return raw_value.get("id") or raw_value.get("email")
            return raw_value.get("email") or raw_value.get("id")
        return raw_value
    if field_type == "multipleCollaborators":
        extract = field_def.get("_extract", "email")
        if isinstance(raw_value, list):
            if extract == "user_id":
                return [
                    (u.get("id") or u.get("email")) if isinstance(u, dict) else u for u in raw_value
                ]
            return [
                (u.get("email") or u.get("id")) if isinstance(u, dict) else u for u in raw_value
            ]
        return raw_value
    if field_type == "aiText":
        # Airtable returns aiText as `{"value": "...", "isStale": bool, ...}`.
        # The replica column is STRING (schema_mapping); extract the value.
        if isinstance(raw_value, dict):
            return raw_value.get("value")
        return raw_value
    return raw_value


def airtable_record_to_bq_row(
    record: dict[str, Any],
    table_name: str,
    table_def: dict[str, Any],
    bq_schema: list[BqField],
    sync_run_id: str,
    synced_at: datetime,
) -> dict[str, Any]:
    """Translate one Airtable record into a BQ row dict.

    Source columns are slugified; system columns are populated from the run
    metadata. Lookup fields are silently dropped (they're not in
    ``bq_schema``).
    """
    fields_in_record = record.get("fields", {})
    by_slug: dict[str, Any] = {}
    for field_def in table_def.get("fields", []):
        if field_def["type"] == "multipleLookupValues":
            continue
        slug = slugify_field_name(field_def["name"])
        if field_def["name"] in fields_in_record:
            by_slug[slug] = _translate_value(field_def, fields_in_record[field_def["name"]])

    # System columns
    by_slug["_airtable_record_id"] = record["id"]
    by_slug["_airtable_table_name"] = table_name
    last_mod_field = LAST_MODIFIED_FIELD_BY_TABLE.get(table_name)
    if last_mod_field and last_mod_field in fields_in_record:
        by_slug["_airtable_last_modified"] = fields_in_record[last_mod_field]
    else:
        # Airtable's per-record metadata ``createdTime`` is always present;
        # for tables without an explicit lastModifiedTime field we fall back
        # to it so ``_airtable_last_modified`` is non-NULL when possible.
        by_slug["_airtable_last_modified"] = record.get("createdTime")
    by_slug["_sync_run_id"] = sync_run_id
    by_slug["_synced_at"] = synced_at.isoformat()
    by_slug["hipaa_excluded"] = False

    # Ensure columns exist with type-appropriate defaults when Airtable
    # legitimately omits the value:
    # - REPEATED arrays (e.g. multipleLookupValues): empty array.
    # - REQUIRED BOOL (checkboxes): False — Airtable's API omits unchecked
    #   checkboxes from the record's `fields` rather than returning False.
    # - Other REQUIRED missing: None. A None here means a genuinely
    #   incomplete record; ``find_invalid_rows`` partitions these out
    #   BEFORE the load so one half-entered record no longer rejects the
    #   whole table's WRITE_TRUNCATE (ADR 0063). Pre-ADR-0063 the None was
    #   left to surface an opaque BQ load error that blackholed the table.
    for col in bq_schema:
        if col.name in by_slug:
            continue
        if col.mode == "REPEATED":
            by_slug[col.name] = []
        elif col.mode == "REQUIRED" and col.type == "BOOL":
            by_slug[col.name] = False
        elif col.mode == "REQUIRED":
            by_slug[col.name] = None
    return by_slug


# ---------------------------------------------------------------------------
# Pre-load required-field validation (ADR 0063)
# ---------------------------------------------------------------------------


@dataclass
class InvalidRow:
    """One record dropped from a load because a REQUIRED field is empty.

    ``label`` is the record's primary-field value (e.g. the Account's
    Company Name) when present, falling back to the record id, so the
    operator can identify the row in Airtable without a lookup.
    ``missing_fields`` carries the human Airtable field names, not slugs.
    """

    record_id: str
    label: str
    missing_fields: tuple[str, ...]


def find_invalid_rows(
    rows: list[dict[str, Any]],
    bq_schema: list[BqField],
    table_def: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[InvalidRow]]:
    """Partition translated rows into (valid, invalid) by REQUIRED-field presence.

    A row is invalid if any REQUIRED, non-REPEATED, non-BOOL column holds
    ``None``. REQUIRED BOOL columns are already defaulted to ``False`` and
    REPEATED to ``[]`` by :func:`airtable_record_to_bq_row`, so neither can
    produce a false positive. System columns (``_airtable_record_id`` etc.)
    are always populated and therefore never flagged.

    Returns the valid rows (safe to load) and a list of :class:`InvalidRow`
    reports for the rest. Per ADR 0063 the caller loads the valid rows and
    logs one ERROR per invalid row — the table keeps syncing while the
    incomplete records nag via the ``asb-cloud-run-job-error`` alert until
    completed in Airtable.
    """
    required_cols = [c.name for c in bq_schema if c.mode == "REQUIRED" and c.type != "BOOL"]
    slug_to_name = {slugify_field_name(f["name"]): f["name"] for f in table_def.get("fields", [])}
    fields = table_def.get("fields", [])
    label_slug = slugify_field_name(fields[0]["name"]) if fields else None

    valid: list[dict[str, Any]] = []
    invalid: list[InvalidRow] = []
    for row in rows:
        missing = [c for c in required_cols if row.get(c) is None]
        if not missing:
            valid.append(row)
            continue
        record_id = row.get("_airtable_record_id", "?")
        label = row.get(label_slug) if label_slug else None
        invalid.append(
            InvalidRow(
                record_id=record_id,
                label=str(label) if label else record_id,
                missing_fields=tuple(slug_to_name.get(c, c) for c in missing),
            )
        )
    return valid, invalid


# ---------------------------------------------------------------------------
# BigQuery load
# ---------------------------------------------------------------------------


def _bq_schema_fields(bq_schema: list[BqField]) -> list[bigquery.SchemaField]:
    from google.cloud import bigquery as _bq

    return [
        _bq.SchemaField(c.name, c.type, mode=c.mode, description=c.description) for c in bq_schema
    ]


def replace_table_contents(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_id: str,
    rows: list[dict[str, Any]],
    bq_schema: list[BqField],
) -> None:
    """Atomically replace ``project.dataset.table`` with ``rows``.

    Uses a load job with ``WRITE_TRUNCATE`` so the table contents flip in one
    BigQuery transaction. An empty ``rows`` list is treated as "the source
    has no non-HIPAA records right now" and truncates the table to empty —
    correct behavior when every Client just got HIPAA-flagged.
    """
    from google.cloud import bigquery as _bq

    table_ref = f"{project_id}.{dataset_id}.{table_id}"
    job_config = _bq.LoadJobConfig(
        schema=_bq_schema_fields(bq_schema),
        write_disposition=_bq.WriteDisposition.WRITE_TRUNCATE,
        source_format=_bq.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    # Even when rows is empty we still issue the load to truncate the table.
    job = bq_client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()  # raises on failure


def upsert_checkpoint(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    airtable_table_name: str,
    run_id: str,
    completed_at: datetime,
) -> None:
    """Write or replace the ``_sync_checkpoints`` row for one source table.

    Uses MERGE (BQ DML) rather than load+truncate so other tables' rows
    survive. Cheap — one DML statement per table per run, well under the
    1500/day per-table DML quota.

    ``last_checkpoint`` stays NULL by design: the sync is full-pull (see
    ``sync_one_table``); the column survives only for table-schema
    compatibility. This row is run bookkeeping (last_run_id / last_run_at).
    """
    from google.cloud import bigquery as _bq

    # ``table_ref`` is built from trusted env config (project_id + dataset_id
    # are constants from terraform-provided env vars), not user input —
    # so the f-string interpolation here is not a SQL injection vector.
    # All actual data values are bound via ScalarQueryParameter below.
    table_ref = f"{project_id}.{dataset_id}.{CHECKPOINTS_TABLE}"
    sql = f"""
        MERGE `{table_ref}` T
        USING (SELECT @table_name AS airtable_table_name) S
        ON T.airtable_table_name = S.airtable_table_name
        WHEN MATCHED THEN UPDATE SET
            last_run_id = @run_id,
            last_run_at = @completed_at
        WHEN NOT MATCHED THEN INSERT (
            airtable_table_name, last_checkpoint, last_run_id, last_run_at
        ) VALUES (
            @table_name, NULL, @run_id, @completed_at
        )
    """  # noqa: S608
    job_config = _bq.QueryJobConfig(
        query_parameters=[
            _bq.ScalarQueryParameter("table_name", "STRING", airtable_table_name),
            _bq.ScalarQueryParameter("run_id", "STRING", run_id),
            _bq.ScalarQueryParameter("completed_at", "TIMESTAMP", completed_at),
        ]
    )
    bq_client.query(sql, job_config=job_config).result()


# ---------------------------------------------------------------------------
# Per-table sync
# ---------------------------------------------------------------------------


def sync_one_table(
    *,
    airtable_table_name: str,
    table_def: dict[str, Any],
    bq_schema: list[BqField],
    airtable_client: AirtableClient,
    bq_client: Any,
    pubsub_publisher: Any,
    drift_topic_path: str,
    project_id: str,
    dataset_id: str,
    sync_run_id: str,
    started_at: datetime,
) -> SyncResult:
    """Run the pipeline for one Airtable table; returns a structured result.

    Always a full pull: the HIPAA clause is the ONLY query filter. Never
    add a time/checkpoint filter here — the load below is WRITE_TRUNCATE,
    so any delta filter truncates the replica to the delta.
    """
    bq_table_id = slugify_table_name(airtable_table_name)
    result = SyncResult(table_name=airtable_table_name)
    table_started = datetime.now(UTC)

    try:
        formula = hipaa_filter_for(airtable_table_name)

        records: list[dict[str, Any]] = list(
            airtable_client.list_records(airtable_table_name, filter_formula=formula)
        )

        # Drift detection: compare observed field names against schema.json.
        # Lookup fields appear in API responses with their resolved value
        # (e.g. ``"Client HIPAA": [false]``); they are codified in
        # schema.json so they count as known.
        observed_fields: set[str] = set()
        for r in records:
            observed_fields.update(r.get("fields", {}).keys())
        known_fields = {f["name"] for f in table_def.get("fields", [])}
        drift_columns = detect_new_columns(airtable_table_name, observed_fields, known_fields)
        if drift_columns:
            event = DriftEvent(
                table_name=airtable_table_name,
                new_columns=drift_columns,
                detected_at=datetime.now(UTC),
            )
            message_id = publish_drift(pubsub_publisher, drift_topic_path, event)
            result.drift_columns = drift_columns
            result.drift_message_ids = (message_id,)
            log.warning(
                "schema drift detected: table=%s new_columns=%s message_id=%s",
                airtable_table_name,
                drift_columns,
                message_id,
            )

        # Translate to BQ rows. Drift columns are intentionally NOT included
        # here — they're not in bq_schema, so airtable_record_to_bq_row drops
        # them. The drift event is the canonical surface; auto-applying would
        # violate PRD §6.2.
        rows = [
            airtable_record_to_bq_row(
                record=r,
                table_name=airtable_table_name,
                table_def=table_def,
                bq_schema=bq_schema,
                sync_run_id=sync_run_id,
                synced_at=started_at,
            )
            for r in records
        ]

        # Pre-load validation (ADR 0063): drop records with empty REQUIRED
        # fields BEFORE the WRITE_TRUNCATE so one half-entered row no longer
        # blackholes the whole table. Each dropped record emits an ERROR log
        # — this trips asb-cloud-run-job-error (the loud nag) and names the
        # exact record + missing fields to fix. The execution itself still
        # exits 0: the sync ran fine and loaded every complete row; the
        # *data* is incomplete, and the log-based alert is the right surface
        # for that. Keeping exit 0 also avoids Cloud Run Job retry storms.
        valid_rows, invalid_rows = find_invalid_rows(rows, bq_schema, table_def)
        for inv in invalid_rows:
            log.error(
                "incomplete record skipped: table=%s record=%s label=%r " "missing_required=%s",
                airtable_table_name,
                inv.record_id,
                inv.label,
                list(inv.missing_fields),
            )

        replace_table_contents(
            bq_client=bq_client,
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=bq_table_id,
            rows=valid_rows,
            bq_schema=bq_schema,
        )
        upsert_checkpoint(
            bq_client=bq_client,
            project_id=project_id,
            dataset_id=dataset_id,
            airtable_table_name=airtable_table_name,
            run_id=sync_run_id,
            completed_at=datetime.now(UTC),
        )
        result.rows_synced = len(valid_rows)
        result.invalid_rows = len(invalid_rows)
    except Exception as exc:
        # One table's failure doesn't poison the others. The orchestrator
        # logs and continues; the next 15-min run retries.
        log.exception("sync failed for table=%s", airtable_table_name)
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.duration_ms = int((datetime.now(UTC) - table_started).total_seconds() * 1000)
    return result


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


def load_schema_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sync(
    *,
    airtable_base_id: str,
    airtable_pat: str,
    project_id: str,
    drift_topic: str,
    schema_json_path: Path,
    dataset_id: str = REPLICA_DATASET_DEFAULT,
    bq_client: Any = None,
    pubsub_publisher: Any = None,
    airtable_session: Any = None,
    tables: Iterable[str] = SYNC_TABLES_ORDER,
) -> SyncRunSummary:
    """Run a full sync cycle. Returns a structured summary for logging.

    Single-base architecture (ADR 0020). The previous multi-base helper
    (``sync_all`` + ``BaseConfig``) was reverted when the CRM base was
    collapsed into Operations.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    summary = SyncRunSummary(run_id=run_id, started_at=started_at)

    schema_json = load_schema_json(schema_json_path)
    table_defs = schema_json["tables"]
    bq_schemas = replica_table_schemas(schema_json_path)

    if bq_client is None:
        from google.cloud import bigquery as _bq

        bq_client = _bq.Client(project=project_id)
    if pubsub_publisher is None:
        from google.cloud import pubsub_v1

        pubsub_publisher = pubsub_v1.PublisherClient()

    drift_topic_path = drift_topic
    if "/" not in drift_topic:
        drift_topic_path = f"projects/{project_id}/topics/{drift_topic}"

    airtable_client = AirtableClient(
        base_id=airtable_base_id, pat=airtable_pat, session=airtable_session
    )

    for airtable_table_name in tables:
        if airtable_table_name not in table_defs:
            log.warning("table %s not in schema.json; skipping", airtable_table_name)
            continue
        result = sync_one_table(
            airtable_table_name=airtable_table_name,
            table_def=table_defs[airtable_table_name],
            bq_schema=bq_schemas[slugify_table_name(airtable_table_name)],
            airtable_client=airtable_client,
            bq_client=bq_client,
            pubsub_publisher=pubsub_publisher,
            drift_topic_path=drift_topic_path,
            project_id=project_id,
            dataset_id=dataset_id,
            sync_run_id=run_id,
            started_at=started_at,
        )
        summary.tables.append(result)

    summary.completed_at = datetime.now(UTC)
    log.info("sync run complete: %s", json.dumps(summary.to_log_dict()))
    return summary


# ---------------------------------------------------------------------------
# CLI / Cloud Run Job entrypoint
# ---------------------------------------------------------------------------


def _read_secret(secret_id: str, project_id: str, sm_client: Any = None) -> str:
    """Fetch the latest version of a Secret Manager secret as a plain string.

    ``secret_id`` is the secret short name (e.g. ``"airtable-pat-prod"``);
    the function builds the canonical path. The Cloud Run Job's SA must hold
    ``roles/secretmanager.secretAccessor`` on this secret only — granted via
    the Terraform binding in ``airtable_sync.tf``.
    """
    if sm_client is None:
        from google.cloud import secretmanager

        sm_client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = sm_client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def main() -> int:
    """Cloud Run Job entrypoint. Reads env vars and calls :func:`sync`.

    Required env vars:
    - ``BRAIN_PROJECT_ID`` — GCP project hosting the replica + topic
    - ``AIRTABLE_BASE_ID`` — the Airtable base to sync
    - ``AIRTABLE_PAT_SECRET_ID`` — Secret Manager short name holding the PAT
    - ``SCHEMA_DRIFT_TOPIC`` — Pub/Sub topic short name (defaults wired in TF)
    """
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    base_id = os.environ["AIRTABLE_BASE_ID"]
    pat_secret_id = os.environ["AIRTABLE_PAT_SECRET_ID"]
    drift_topic = os.environ.get("SCHEMA_DRIFT_TOPIC", "asb-schema-drift-alerts")

    pat = _read_secret(pat_secret_id, project_id)
    schema_json_path = Path(__file__).resolve().parents[3] / "airtable" / "schema.json"
    if not schema_json_path.exists():
        # Container layout: schema.json is copied to /app/airtable/schema.json
        # alongside src/. Fall back to that if the dev-env relative path misses.
        schema_json_path = Path("/app/airtable/schema.json")

    summary = sync(
        airtable_base_id=base_id,
        airtable_pat=pat,
        project_id=project_id,
        drift_topic=drift_topic,
        schema_json_path=schema_json_path,
    )
    failed = [t for t in summary.tables if t.error]
    if failed:
        log.error("sync completed with %d table failure(s)", len(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
