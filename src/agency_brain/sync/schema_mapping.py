"""Map Airtable schema definitions to BigQuery schema fields.

Single source of truth: ``airtable/schema.json``. The Airtable base does not
exist yet; this file translates the codified schema into the column layout the
``airtable_replica.*`` tables get in BigQuery (PRD §6.2).

Two callers:

- ``airtable_to_bq.py`` builds the MERGE / DELETE statements from the BQ
  schemas returned here.
- ``replica_tables.tf`` reads ``airtable/schema.json`` directly via
  ``jsondecode`` and builds the same column layout in HCL — these two paths
  must stay aligned. The snapshot tests in
  ``tests/unit/sync/test_schema_mapping.py`` are the load-bearing alignment
  check.

The HIPAA Lookup fields (``Projects.Client HIPAA``, ``Tasks.Project HIPAA``)
are intentionally omitted from the replica: they exist solely to drive
``filterByFormula`` at the Airtable API layer (PRD §4.1 layer 2). The replica
already carries the Client/Project link IDs if a downstream caller needs to
re-derive HIPAA status, and HIPAA-flagged rows never land in the replica in
the first place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Airtable field type → (BigQuery field type, mode)
#
# ``multipleSelects`` and ``multipleRecordLinks`` arrive from the Airtable API
# as JSON arrays; BigQuery REPEATED STRING is the natural fit. ``number``
# uses FLOAT64 because Airtable does not expose precision on the number field
# and Goal Scores / future numeric fields don't carry monetary semantics.
_TYPE_MAP: dict[str, tuple[str, str]] = {
    "singleLineText": ("STRING", "NULLABLE"),
    "multilineText": ("STRING", "NULLABLE"),
    # richText: same shape as multilineText (Airtable returns Markdown string).
    # Surfaced 2026-05-28 via the audit drift script — Accounts/Contacts.
    # Pending Updates auto-upgraded to richText in the Airtable UI.
    "richText": ("STRING", "NULLABLE"),
    "singleSelect": ("STRING", "NULLABLE"),
    "email": ("STRING", "NULLABLE"),
    "url": ("STRING", "NULLABLE"),
    "phoneNumber": ("STRING", "NULLABLE"),
    # aiText: Airtable AI-generated columns (e.g. CRM Accounts."Industry (AI)").
    # Replicated as plain text — the Brain doesn't write these, just reads.
    "aiText": ("STRING", "NULLABLE"),
    "multipleSelects": ("STRING", "REPEATED"),
    "multipleRecordLinks": ("STRING", "REPEATED"),
    "checkbox": ("BOOL", "NULLABLE"),
    "date": ("DATE", "NULLABLE"),
    "dateTime": ("TIMESTAMP", "NULLABLE"),
    "lastModifiedTime": ("TIMESTAMP", "NULLABLE"),
    "createdTime": ("TIMESTAMP", "NULLABLE"),
    "number": ("FLOAT64", "NULLABLE"),
    "percent": ("FLOAT64", "NULLABLE"),
    "currency": ("FLOAT64", "NULLABLE"),
    # Collaborator fields: Airtable returns the user as an object;
    # _translate_value in airtable_to_bq.py extracts the email so the BQ value
    # is a STRING that joins on Team.workspace_email.
    "singleCollaborator": ("STRING", "NULLABLE"),
    "multipleCollaborators": ("STRING", "REPEATED"),
    # count: Airtable's Count field returns the integer count of linked records
    # matching an optional filter; useful for replica-side dashboards.
    "count": ("INT64", "NULLABLE"),
    # autoNumber: Airtable's auto-incrementing integer ID. Always populated
    # by Airtable, but mapped NULLABLE here so a row's initial pre-sync
    # state (or a recovery path) can't crash the load. Surfaced via the
    # 2026-05-28 audit drift script on Captures.Id.
    "autoNumber": ("INT64", "NULLABLE"),
}

# Field types that exist in Airtable purely as derived metadata and are NOT
# replicated to BigQuery. Lookup fields are filter-only (see module docstring).
_LOOKUP_TYPE = "multipleLookupValues"

# System columns added by the sync layer. Column order here matches the order
# replica tables get in BigQuery — ``airtable_to_bq.py`` and
# ``replica_tables.tf`` both rely on this list.
_SYSTEM_COLUMNS: tuple[dict[str, str], ...] = (
    {
        "name": "_airtable_record_id",
        "type": "STRING",
        "mode": "REQUIRED",
        "description": "Airtable record ID (recXXXXX). Primary key for MERGE.",
    },
    {
        "name": "_airtable_table_name",
        "type": "STRING",
        "mode": "REQUIRED",
        "description": "Source Airtable table name (e.g. 'Clients').",
    },
    {
        "name": "_airtable_last_modified",
        "type": "TIMESTAMP",
        "mode": "NULLABLE",
        "description": "Airtable's lastModifiedTime for the record. Drives incremental delta.",
    },
    {
        "name": "_sync_run_id",
        "type": "STRING",
        "mode": "REQUIRED",
        "description": "UUID for the sync run that wrote this row. Operational debugging.",
    },
    {
        "name": "_synced_at",
        "type": "TIMESTAMP",
        "mode": "REQUIRED",
        "description": "When the sync run wrote this row.",
    },
    {
        "name": "hipaa_excluded",
        "type": "BOOL",
        "mode": "REQUIRED",
        "description": (
            "Always FALSE in the replica — HIPAA-flagged rows are filtered at "
            "the Airtable query (PRD §4.1 layer 2). Column exists so downstream "
            "views can carry the canonical clause "
            "COALESCE(hipaa_excluded, FALSE) = FALSE that hipaa_filter_check.py "
            "enforces (PRD §4.8)."
        ),
    },
)


@dataclass(frozen=True)
class BqField:
    """Plain dict-able BigQuery field.

    Kept independent of ``google.cloud.bigquery.SchemaField`` so the module is
    importable without the BigQuery client (tests, Terraform-side validation).
    """

    name: str
    type: str
    mode: str
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "mode": self.mode}
        if self.description:
            out["description"] = self.description
        return out


class UnsupportedAirtableTypeError(ValueError):
    """Raised when an Airtable field type has no mapping.

    Catches schema additions that haven't been thought through. The fix is
    either to add the type to ``_TYPE_MAP`` or to mark it as filter-only like
    ``multipleLookupValues``.
    """


def slugify_table_name(airtable_name: str) -> str:
    """``"Goal Scores"`` → ``"goal_scores"``.

    BigQuery table IDs must be ``[a-zA-Z0-9_]``; Airtable table names allow
    spaces and most punctuation. The slug is lowercased to keep replica table
    IDs predictable from code and Terraform.
    """
    s = airtable_name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def slugify_field_name(airtable_name: str) -> str:
    """``"Client Name"`` → ``"client_name"``.

    Same rules as ``slugify_table_name`` — kept as a separate function because
    field-naming may diverge from table-naming later (e.g. preserving case for
    abbreviations). For now they share an implementation.
    """
    return slugify_table_name(airtable_name)


def airtable_field_to_bq_field(field_def: dict[str, Any]) -> BqField | None:
    """Translate one Airtable field definition into a BqField.

    Returns ``None`` for filter-only field types (Lookups) so callers can
    iterate the schema without special-casing them.

    Raises ``UnsupportedAirtableTypeError`` for unmapped types.
    """
    airtable_type = field_def["type"]
    if airtable_type == _LOOKUP_TYPE:
        return None

    mapping = _TYPE_MAP.get(airtable_type)
    if mapping is None:
        raise UnsupportedAirtableTypeError(
            f"Airtable type {airtable_type!r} (field {field_def.get('name')!r}) has no "
            "BQ mapping. Add it to schema_mapping._TYPE_MAP or mark it filter-only."
        )

    bq_type, default_mode = mapping
    mode = "REQUIRED" if field_def.get("required") else default_mode
    # REPEATED never carries REQUIRED — BigQuery rejects the combination.
    if default_mode == "REPEATED":
        mode = "REPEATED"

    return BqField(
        name=slugify_field_name(field_def["name"]),
        type=bq_type,
        mode=mode,
        description=field_def.get("notes"),
    )


def system_columns() -> list[BqField]:
    """Columns added to every replica table by the sync layer."""
    return [
        BqField(
            name=col["name"],
            type=col["type"],
            mode=col["mode"],
            description=col["description"],
        )
        for col in _SYSTEM_COLUMNS
    ]


def table_schema(airtable_table_def: dict[str, Any]) -> list[BqField]:
    """Full BQ column list for one replica table: source columns + system columns.

    Source columns appear first in the order they're declared in
    ``airtable/schema.json``; system columns follow.
    """
    source: list[BqField] = []
    for field_def in airtable_table_def.get("fields", []):
        bq_field = airtable_field_to_bq_field(field_def)
        if bq_field is None:
            continue
        source.append(bq_field)
    return source + system_columns()


def replica_table_schemas(schema_json_path: Path) -> dict[str, list[BqField]]:
    """Load ``airtable/schema.json`` and return one schema per replica table.

    Keyed by the BQ table ID (slugified). Single-base architecture per
    ADR 0020 (supersedes the multi-base helper from ADR 0018).
    """
    schema = json.loads(schema_json_path.read_text())
    out: dict[str, list[BqField]] = {}
    for airtable_name, table_def in schema.get("tables", {}).items():
        out[slugify_table_name(airtable_name)] = table_schema(table_def)
    return out


def checkpoint_table_schema() -> list[BqField]:
    """Schema for ``airtable_replica._sync_checkpoints``.

    One row per source Airtable table, holding run bookkeeping
    (``last_run_id`` / ``last_run_at``). ``last_checkpoint`` stays NULL:
    the sync is full-pull (WRITE_TRUNCATE), so there is no incremental
    delta to checkpoint. Column retained for schema stability. ADR 0010.
    """
    return [
        BqField(
            name="airtable_table_name",
            type="STRING",
            mode="REQUIRED",
            description="Airtable table name (e.g. 'Clients'). Primary key.",
        ),
        BqField(
            name="last_checkpoint",
            type="TIMESTAMP",
            mode="NULLABLE",
            description="Max(_airtable_last_modified) successfully merged. NULL on first run.",
        ),
        BqField(
            name="last_run_id",
            type="STRING",
            mode="NULLABLE",
            description="UUID of the last sync run that wrote this row.",
        ),
        BqField(
            name="last_run_at",
            type="TIMESTAMP",
            mode="NULLABLE",
            description="When the last sync run completed for this table.",
        ),
    ]
