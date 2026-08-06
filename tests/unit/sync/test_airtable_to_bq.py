"""Pre-load required-field validation (ADR 0063).

``find_invalid_rows`` partitions translated BQ rows so one half-entered
Airtable record no longer blackholes the whole table's WRITE_TRUNCATE load.
"""

from __future__ import annotations

from agency_brain.sync.airtable_to_bq import find_invalid_rows
from agency_brain.sync.schema_mapping import BqField

# A trimmed Accounts-shaped table_def + schema mirroring the real fields that
# bit us on 2026-06-05 (Houston Gilbert: empty Segment / Status / Account
# Manager). Company Name is the primary field (first), so it supplies the
# human label.
TABLE_DEF = {
    "fields": [
        {"name": "Company Name", "type": "singleLineText"},
        {"name": "Segment", "type": "singleSelect", "required": True},
        {"name": "Status", "type": "singleSelect", "required": True},
        {"name": "Account Manager", "type": "singleCollaborator", "required": True},
        {"name": "HIPAA", "type": "checkbox", "required": True},
        {"name": "Contacts", "type": "multipleLookupValues"},
    ]
}

BQ_SCHEMA = [
    BqField("company_name", "STRING", "REQUIRED"),
    BqField("segment", "STRING", "REQUIRED"),
    BqField("status", "STRING", "REQUIRED"),
    BqField("account_manager", "STRING", "REQUIRED"),
    BqField("hipaa", "BOOL", "REQUIRED"),
    BqField("contacts", "STRING", "REPEATED"),
    BqField("_airtable_record_id", "STRING", "REQUIRED"),
    BqField("hipaa_excluded", "BOOL", "REQUIRED"),
]


def _row(**overrides):
    """A fully-populated, valid translated row; override to break it."""
    base = {
        "company_name": "Acme Corp",
        "segment": "E-commerce",
        "status": "Active",
        "account_manager": "owner@example.com",
        "hipaa": False,
        "contacts": [],
        "_airtable_record_id": "recAAAAAAAAAAAAAA",
        "hipaa_excluded": False,
    }
    base.update(overrides)
    return base


def test_all_valid_rows_pass_through():
    rows = [_row(), _row(_airtable_record_id="recBBBBBBBBBBBBBB")]
    valid, invalid = find_invalid_rows(rows, BQ_SCHEMA, TABLE_DEF)
    assert len(valid) == 2
    assert invalid == []


def test_missing_required_field_is_partitioned_out():
    rows = [
        _row(),
        _row(
            _airtable_record_id="recJpl1Y2ePpARd0i",
            company_name="Houston Gilbert",
            segment=None,
            status=None,
            account_manager=None,
        ),
    ]
    valid, invalid = find_invalid_rows(rows, BQ_SCHEMA, TABLE_DEF)

    assert len(valid) == 1
    assert valid[0]["company_name"] == "Acme Corp"

    assert len(invalid) == 1
    inv = invalid[0]
    assert inv.record_id == "recJpl1Y2ePpARd0i"
    # Human label comes from the primary field, not the slug or record id.
    assert inv.label == "Houston Gilbert"
    # Reported by Airtable field name (not slug), in schema order, all of them.
    assert inv.missing_fields == ("Segment", "Status", "Account Manager")


def test_required_bool_unset_is_not_flagged():
    # REQUIRED BOOL (HIPAA) defaults to False upstream; None can't occur, but
    # even an explicit False must never count as "missing".
    valid, invalid = find_invalid_rows([_row(hipaa=False)], BQ_SCHEMA, TABLE_DEF)
    assert len(valid) == 1
    assert invalid == []


def test_repeated_empty_array_is_not_flagged():
    valid, invalid = find_invalid_rows([_row(contacts=[])], BQ_SCHEMA, TABLE_DEF)
    assert len(valid) == 1
    assert invalid == []


def test_label_falls_back_to_record_id_when_primary_empty():
    rows = [_row(company_name=None, segment=None)]
    _valid, invalid = find_invalid_rows(rows, BQ_SCHEMA, TABLE_DEF)
    assert len(invalid) == 1
    # Company Name (primary) is itself missing, so label falls back to the id.
    assert invalid[0].label == "recAAAAAAAAAAAAAA"
    assert "Company Name" in invalid[0].missing_fields
    assert "Segment" in invalid[0].missing_fields


def test_system_required_columns_never_false_positive():
    # _airtable_record_id / hipaa_excluded are REQUIRED but always populated by
    # airtable_record_to_bq_row, so a clean row stays valid.
    valid, invalid = find_invalid_rows([_row()], BQ_SCHEMA, TABLE_DEF)
    assert len(valid) == 1
    assert invalid == []
