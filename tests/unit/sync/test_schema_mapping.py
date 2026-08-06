"""Snapshot test: ``airtable/schema.json`` derives the BQ replica column layout.

The Terraform side (``terraform/modules/data_pipeline/replica_tables.tf``)
reads the same JSON and must produce the same columns. If a future schema
change shifts these snapshots, the Terraform-side jsonencode block needs the
matching update — the snapshot is the cross-language alignment check.

Lookup fields (``Projects.Client HIPAA``, ``Tasks.Project HIPAA``) are
intentionally absent from the snapshot — they live only on the Airtable side
to drive ``filterByFormula`` (PRD §4.1 layer 2).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agency_brain.sync.schema_mapping import (
    BqField,
    UnsupportedAirtableTypeError,
    airtable_field_to_bq_field,
    checkpoint_table_schema,
    replica_table_schemas,
    slugify_field_name,
    slugify_table_name,
    system_columns,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_JSON = REPO_ROOT / "airtable" / "schema.json"
REPLICA_TABLES_TF = REPO_ROOT / "terraform" / "modules" / "data_pipeline" / "replica_tables.tf"


def test_slugify_table_name():
    assert slugify_table_name("Clients") == "clients"
    assert slugify_table_name("Goal Scores") == "goal_scores"
    assert slugify_table_name("Risk Profiles") == "risk_profiles"


def test_slugify_field_name():
    assert slugify_field_name("Client Name") == "client_name"
    assert slugify_field_name("HIPAA") == "hipaa"
    assert slugify_field_name("Account Manager") == "account_manager"


def test_simple_text_field_maps_to_nullable_string():
    field = airtable_field_to_bq_field({"name": "Notes", "type": "multilineText"})
    assert field == BqField(name="notes", type="STRING", mode="NULLABLE")


def test_required_text_field_maps_to_required_string():
    field = airtable_field_to_bq_field(
        {"name": "Client Name", "type": "singleLineText", "required": True}
    )
    assert field == BqField(name="client_name", type="STRING", mode="REQUIRED")


def test_checkbox_maps_to_bool():
    field = airtable_field_to_bq_field({"name": "Active", "type": "checkbox", "required": True})
    assert field == BqField(name="active", type="BOOL", mode="REQUIRED")


def test_multiple_select_maps_to_repeated_string():
    # REPEATED beats REQUIRED — BigQuery rejects REPEATED + REQUIRED together,
    # so the required flag on a multipleSelects field is silently dropped.
    field = airtable_field_to_bq_field(
        {"name": "Tags", "type": "multipleSelects", "required": True}
    )
    assert field == BqField(name="tags", type="STRING", mode="REPEATED")


def test_multiple_record_links_maps_to_repeated_string():
    field = airtable_field_to_bq_field(
        {"name": "Owner", "type": "multipleRecordLinks", "linked_table": "Team"}
    )
    assert field == BqField(name="owner", type="STRING", mode="REPEATED")


def test_date_and_timestamp_types():
    assert airtable_field_to_bq_field({"name": "Due Date", "type": "date"}) == BqField(
        name="due_date", type="DATE", mode="NULLABLE"
    )
    assert airtable_field_to_bq_field({"name": "Created", "type": "createdTime"}) == BqField(
        name="created", type="TIMESTAMP", mode="NULLABLE"
    )
    assert airtable_field_to_bq_field(
        {"name": "Last Activity Timestamp", "type": "lastModifiedTime"}
    ) == BqField(name="last_activity_timestamp", type="TIMESTAMP", mode="NULLABLE")


def test_number_maps_to_float64():
    field = airtable_field_to_bq_field({"name": "Score", "type": "number", "required": True})
    assert field == BqField(name="score", type="FLOAT64", mode="REQUIRED")


def test_lookup_field_returns_none():
    # Lookup fields are filter-only; the orchestrator must not write them.
    assert (
        airtable_field_to_bq_field({"name": "Client HIPAA", "type": "multipleLookupValues"}) is None
    )


def test_unknown_field_type_raises():
    with pytest.raises(UnsupportedAirtableTypeError):
        airtable_field_to_bq_field({"name": "Mystery", "type": "rollup"})


def test_system_columns_present_and_ordered():
    cols = [c.name for c in system_columns()]
    assert cols == [
        "_airtable_record_id",
        "_airtable_table_name",
        "_airtable_last_modified",
        "_sync_run_id",
        "_synced_at",
        "hipaa_excluded",
    ]


def test_replica_table_schemas_keys_match_expected_tables():
    schemas = replica_table_schemas(SCHEMA_JSON)
    # Single-base architecture (ADR 0020). Accounts/Contacts/Contracts
    # replace the legacy Clients table.
    assert set(schemas.keys()) == {
        "accounts",
        "contacts",
        "contracts",
        "projects",
        "tasks",
        "goals",
        "goal_scores",
        "team",
        "risk_profiles",
        "service_catalog",
        # ADR 0037 §3 — Captures form target for the PKM merge.
        "captures",
        # Audit finding F7 (2026-05-28) — developer-workflow inbox.
        "orchestrator_inbox",
    }


def test_aitext_field_maps_to_string():
    """aiText (Airtable AI columns) replicates as plain STRING NULLABLE."""
    field = airtable_field_to_bq_field({"name": "Industry (AI)", "type": "aiText"})
    assert field == BqField(name="industry_ai", type="STRING", mode="NULLABLE")


def test_accounts_schema_includes_hipaa_and_omits_lookup():
    schemas = replica_table_schemas(SCHEMA_JSON)
    accounts = {f.name: f for f in schemas["accounts"]}
    assert accounts["hipaa"] == BqField(
        name="hipaa",
        type="BOOL",
        mode="REQUIRED",
        description=accounts["hipaa"].description,
    )
    # Verify Projects' Account HIPAA lookup is filtered out.
    projects = {f.name: f for f in schemas["projects"]}
    assert (
        "account_hipaa" not in projects
    ), "Lookup field Projects.Account HIPAA must NOT be replicated to BQ"


def test_contacts_has_account_link_for_sender_resolver():
    """The Triage Agent's sender resolver depends on Contacts.Email and
    Contacts.Account (the join target into sender_to_project_v)."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["contacts"]}
    assert fields["email"].type == "STRING"
    assert fields["account"].mode == "REPEATED"
    assert fields["account"].type == "STRING"
    # HIPAA cascade lookup is filtered out (it's a multipleLookupValues field).
    assert "account_hipaa" not in fields


def test_contracts_has_account_link():
    """Contracts links to Accounts via the Account field — replaces the
    legacy Source Contract Record ID text-field cross-base reference."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["contracts"]}
    assert fields["account"].mode == "REPEATED"
    assert fields["account"].type == "STRING"


def test_tasks_schema_omits_lookup_field():
    schemas = replica_table_schemas(SCHEMA_JSON)
    tasks = {f.name: f for f in schemas["tasks"]}
    assert (
        "project_hipaa" not in tasks
    ), "Lookup field Tasks.Project HIPAA must NOT be replicated to BQ"


def test_every_replica_table_has_system_columns():
    schemas = replica_table_schemas(SCHEMA_JSON)
    for table_id, fields in schemas.items():
        names = {f.name for f in fields}
        for required in (
            "_airtable_record_id",
            "_airtable_table_name",
            "_sync_run_id",
            "_synced_at",
            "hipaa_excluded",
        ):
            assert required in names, f"{table_id} missing required system column {required}"


def test_checkpoint_table_schema_shape():
    cols = {f.name: f for f in checkpoint_table_schema()}
    assert cols["airtable_table_name"].mode == "REQUIRED"
    assert cols["last_checkpoint"].type == "TIMESTAMP"
    assert cols["last_run_id"].type == "STRING"
    assert cols["last_run_at"].type == "TIMESTAMP"


def test_accounts_has_load_bearing_join_keys():
    """Accounts is the canonical client record (post-ADR-0020 collapse).
    Carries the HIPAA flag, Risk Watcher segment selector, operational
    Status, and Account Manager — the fields downstream code joins on."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["accounts"]}
    # Risk Watcher subclass selector
    assert fields["segment"].mode == "REQUIRED"
    assert fields["segment"].type == "STRING"
    # Status drives Project lifecycle and Renewal Window alerting
    assert fields["status"].mode == "REQUIRED"
    # HIPAA flag — load-bearing per PRD §4.1 layer 2
    assert fields["hipaa"] == BqField(
        name="hipaa",
        type="BOOL",
        mode="REQUIRED",
        description=fields["hipaa"].description,
    )
    # Account Manager — Brain's ownership default for spawned Projects
    assert fields["account_manager"].type == "STRING"


def test_projects_has_load_bearing_join_keys():
    """Projects carries the Account link (replaces Client + Source Contract
    Record ID per ADR 0020), Service link, Contract link, and operational state."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["projects"]}
    # Linked records (REPEATED arrays of recXXX)
    assert fields["account"].mode == "REPEATED"
    assert fields["service"].mode == "REPEATED"
    assert fields["contract"].mode == "REPEATED"  # Optional but always REPEATED
    # Operational state
    assert fields["phase"].mode == "REQUIRED"
    assert fields["status"].mode == "REQUIRED"
    assert fields["health"].type == "STRING"  # NULLABLE — Health is optional
    # Owner — Brain-drafted Tasks default to this
    assert fields["owner"].type == "STRING"
    # Lookup field MUST NOT be replicated
    assert (
        "account_hipaa" not in fields
    ), "Lookup field Projects.Account HIPAA must NOT be replicated to BQ"


def test_tasks_has_load_bearing_join_keys():
    """Tasks is the Brain's primary write target. Approval Status is what
    routes drafts vs. approved Tasks."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["tasks"]}
    # Project link is REQUIRED — drives HIPAA cascade and ownership
    assert fields["project"].mode == "REPEATED"  # Airtable link is always repeated
    # Source distinguishes Manual vs Brain-drafted. NULLABLE since PR #170
    # (Phase 0 Bug 4) — CRM Auto-updater drafts arrive with a blank Source
    # and the human picks it at approval time, matching the action_type
    # pattern below.
    assert fields["source"].mode == "NULLABLE"
    # Approval Status — load-bearing for WS-D routing
    assert fields["approval_status"].mode == "REQUIRED"
    # Action Type — NULLABLE because CRM Auto-updater drafts arrive with
    # Approval Status = 'Drafted by Agent' and a blank Action Type (the
    # human picks the GTD bucket at approval). Required at the Airtable
    # UI level (validation_rules.md) but allowed NULL at the sync/BQ layer.
    assert fields["action_type"].mode == "NULLABLE"
    # Status — operational state
    assert fields["status"].mode == "REQUIRED"
    # Owner — required
    assert fields["owner"].type == "STRING"
    # Lookup field MUST NOT be replicated
    assert (
        "project_hipaa" not in fields
    ), "Lookup field Tasks.Project HIPAA must NOT be replicated to BQ"


def test_goals_supports_hierarchy():
    """Parent Goal is the hierarchy spine. Owner is who Goal Steward prompts."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["goals"]}
    assert fields["horizon"].mode == "REQUIRED"
    assert fields["parent_goal"].mode == "REPEATED"  # Self-link, always repeated
    assert fields["status"].mode == "REQUIRED"


def test_goal_scores_has_score_and_week():
    """Goal Steward writes one row per goal per week."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["goal_scores"]}
    assert fields["goal"].mode == "REPEATED"
    assert fields["week_of"].mode == "REQUIRED"
    assert fields["week_of"].type == "DATE"
    assert fields["score"].mode == "REQUIRED"
    assert fields["score"].type == "FLOAT64"


def test_team_has_workspace_email_join_key():
    """Workspace Email is THE join key the Brain uses to map Airtable
    User collaborator fields to Team rows."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["team"]}
    assert fields["workspace_email"] == BqField(
        name="workspace_email",
        type="STRING",
        mode="REQUIRED",
        description=fields["workspace_email"].description,
    )
    assert fields["role"].mode == "REQUIRED"
    assert fields["active"].type == "BOOL"
    assert fields["active"].mode == "REQUIRED"


def test_team_user_field_present_with_extract_user_id_annotation():
    """Team.User carries the Airtable usrXXX collaborator id (ADR 0019)."""
    import json

    schema = json.loads(SCHEMA_JSON.read_text())
    team_fields = schema["tables"]["Team"]["fields"]
    user_field = next((f for f in team_fields if f["name"] == "User"), None)
    assert user_field is not None, "Team must declare a User singleCollaborator field"
    assert user_field["type"] == "singleCollaborator"
    assert user_field.get("_extract") == "user_id"


def test_translate_value_extract_user_id_returns_id_not_email():
    """ADR 0019: singleCollaborator with _extract: user_id yields the usrXXX."""
    from agency_brain.sync.airtable_to_bq import _translate_value

    field_def = {"name": "User", "type": "singleCollaborator", "_extract": "user_id"}
    raw = {"id": "usrthe operator", "email": "owner@example.com", "name": "the operator"}
    assert _translate_value(field_def, raw) == "usrthe operator"


def test_translate_value_extract_email_default_unchanged():
    """Default behavior: extract email. Unchanged for back-compat."""
    from agency_brain.sync.airtable_to_bq import _translate_value

    field_def = {"name": "Owner", "type": "singleCollaborator"}
    raw = {"id": "usrthe operator", "email": "owner@example.com", "name": "the operator"}
    assert _translate_value(field_def, raw) == "owner@example.com"


def test_translate_value_aitext_extracts_value_string():
    """aiText returns a dict {value, isStale, ...}; replica column is STRING."""
    from agency_brain.sync.airtable_to_bq import _translate_value

    field_def = {"name": "Industry (AI)", "type": "aiText"}
    raw = {"value": "Illustration & Design Services", "isStale": False}
    assert _translate_value(field_def, raw) == "Illustration & Design Services"


def test_required_checkbox_defaults_to_false_when_unchecked():
    """Airtable omits unchecked checkboxes from the record's `fields` dict.
    The row builder must default REQUIRED BOOL columns to False so BQ
    doesn't reject the load."""
    from datetime import UTC, datetime

    from agency_brain.sync.airtable_to_bq import airtable_record_to_bq_row
    from agency_brain.sync.schema_mapping import BqField

    record = {
        "id": "recAcc01",
        "createdTime": "2026-04-30T00:00:00Z",
        "fields": {"Company Name": "Test"},
    }
    table_def = {
        "fields": [
            {"name": "Company Name", "type": "singleLineText", "required": True},
            {"name": "HIPAA", "type": "checkbox", "required": True},
        ]
    }
    bq_schema = [
        BqField(name="company_name", type="STRING", mode="REQUIRED"),
        BqField(name="hipaa", type="BOOL", mode="REQUIRED"),
    ]
    row = airtable_record_to_bq_row(
        record=record,
        table_name="Accounts",
        table_def=table_def,
        bq_schema=bq_schema,
        sync_run_id="run-1",
        synced_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert row["hipaa"] is False, "Unchecked checkbox should default to False, not None"
    assert row["company_name"] == "Test"


def test_service_catalog_has_canonical_fields():
    """Service Catalog is the single source of truth for service offerings.
    Service Code is the unique identifier Brain code keys on."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["service_catalog"]}
    assert fields["service_name"].mode == "REQUIRED"
    assert fields["service_code"].mode == "REQUIRED"
    assert fields["category"].mode == "REQUIRED"
    assert fields["active"].type == "BOOL"
    assert fields["active"].mode == "REQUIRED"


def test_risk_profiles_has_pattern_config():
    """Risk Watcher reads these rows on every invocation."""
    schemas = replica_table_schemas(SCHEMA_JSON)
    fields = {f.name: f for f in schemas["risk_profiles"]}
    assert fields["pattern_name"].mode == "REQUIRED"
    assert fields["segment"].mode == "REQUIRED"
    assert fields["severity_default"].mode == "REQUIRED"
    assert fields["active"].mode == "REQUIRED"


def test_collaborator_types_map_to_string():
    """Collaborator field types translate to STRING/STRING REPEATED in BQ.
    The translator in airtable_to_bq._translate_value extracts the email
    from the Airtable user object so the BQ value is the email string."""
    from agency_brain.sync.schema_mapping import airtable_field_to_bq_field

    single = airtable_field_to_bq_field({"name": "Owner", "type": "singleCollaborator"})
    assert single.type == "STRING"
    assert single.mode == "NULLABLE"

    multi = airtable_field_to_bq_field({"name": "Contributors", "type": "multipleCollaborators"})
    assert multi.type == "STRING"
    assert multi.mode == "REPEATED"


def test_count_field_maps_to_int64():
    """Count field (rollup of linked-record matches) maps to INT64 in BQ."""
    from agency_brain.sync.schema_mapping import airtable_field_to_bq_field

    field = airtable_field_to_bq_field({"name": "Open Tasks Count", "type": "count"})
    assert field.type == "INT64"
    assert field.mode == "NULLABLE"


# ---------------------------------------------------------------------------
# Cross-language alignment — Python <-> Terraform
# ---------------------------------------------------------------------------


_TF_SYSTEM_BLOCK_RE = re.compile(
    r"system_columns\s*=\s*\[\s*\n(?P<body>.*?)\n\s*\]",
    re.DOTALL,
)
_TF_ITEM_RE = re.compile(r"\{(?P<inner>[^{}]+?)\}", re.DOTALL)
_TF_FIELD_RE = re.compile(r'(\w+)\s*=\s*"(.*?)"', re.DOTALL)


def _parse_tf_system_columns(tf_path: Path) -> list[dict[str, str]]:
    """Pull the literal `system_columns = [ {...}, ... ]` block out of HCL.

    Each system column in `replica_tables.tf` is a flat object with four
    string fields (name, type, mode, description), so a small regex is
    enough — no need for `python-hcl2`. If the HCL formatting ever drifts
    away from the current style, this parser will yield zero items and the
    snapshot test will fail loudly, which is the desired behavior.
    """
    text = tf_path.read_text()
    block = _TF_SYSTEM_BLOCK_RE.search(text)
    if not block:
        raise AssertionError(
            f"Could not locate `system_columns = [...]` block in {tf_path}. "
            "If the block was renamed or restructured, update "
            "_TF_SYSTEM_BLOCK_RE in this test."
        )
    items: list[dict[str, str]] = []
    for match in _TF_ITEM_RE.finditer(block.group("body")):
        fields = dict(_TF_FIELD_RE.findall(match.group("inner")))
        items.append(fields)
    return items


def test_full_schema_snapshot():
    """Cross-language alignment: Python ``_SYSTEM_COLUMNS`` and Terraform
    ``local.system_columns`` must match on (name, type, mode, description).

    PR #12 caught system-column descriptions in ``replica_tables.tf`` being
    silently shortened from their Python counterparts. The other
    ``test_schema_mapping`` cases assert names, types, and modes; this test
    is the one that locks in the description strings so that drift fails CI
    instead of leaking into ``terraform plan`` diffs.

    Airtable-derived columns aren't compared here: both sides pull
    descriptions from the same ``airtable/schema.json`` ``notes`` field, so
    they're aligned by construction. The data-flow guard at the end of this
    test asserts that path is still wired up.
    """
    py_cols = [(c.name, c.type, c.mode, c.description) for c in system_columns()]
    tf_items = _parse_tf_system_columns(REPLICA_TABLES_TF)
    tf_cols = [(item["name"], item["type"], item["mode"], item["description"]) for item in tf_items]

    assert tf_cols == py_cols, (
        "System-column drift between Python schema_mapping._SYSTEM_COLUMNS and "
        "Terraform local.system_columns. Update both sides in lockstep."
    )

    # Data-flow guard: the line that bridges airtable/schema.json `notes`
    # into the per-field BQ description in TF. If someone renames `notes` on
    # the JSON side or changes the try() call, this catches it before plan.
    tf_text = REPLICA_TABLES_TF.read_text()
    assert "description = try(f.notes, null)" in tf_text, (
        "Terraform-side replica field descriptions must derive from "
        "airtable/schema.json `notes`. Expected the literal "
        "`description = try(f.notes, null)` line in replica_tables.tf."
    )
