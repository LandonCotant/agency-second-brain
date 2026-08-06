"""Build the ``filterByFormula`` clauses applied at the Airtable API layer.

PRD §4.1 layer 2 mandates the HIPAA cascade happens at the source query, not
in post-processing. Single-base architecture (ADR 0020) — the cascade roots
at ``Accounts.HIPAA`` and propagates via Airtable Lookup fields codified in
``airtable/schema.json``:

- ``Contacts.Account HIPAA`` looks up ``Accounts.HIPAA`` via the ``Account`` link.
- ``Contracts.Account HIPAA`` looks up ``Accounts.HIPAA`` via the ``Account`` link.
- ``Projects.Account HIPAA`` looks up ``Accounts.HIPAA`` via the ``Account`` link.
- ``Tasks.Project HIPAA`` looks up ``Projects.Account HIPAA`` via the ``Project``
  link, transitively reflecting the Account's flag.

The orchestrator combines the HIPAA clause with the ``last_modified >
checkpoint`` clause via ``AND(...)`` for the incremental delta pull, and uses
the HIPAA clause alone for the full-set pull that drives the
``DELETE FROM ... WHERE _airtable_record_id NOT IN (...)`` removal pass.

Tables with no account linkage (``Team``, ``Goals``, ``Goal Scores``,
``Risk Profiles``, ``Service Catalog``, ``Captures``) get a permissive
``TRUE()`` so the orchestrator can call ``hipaa_filter_for(table_name)``
uniformly without per-table branches.
"""

from __future__ import annotations

# The mapping is declared once so the test suite can introspect it and assert
# every table is covered. Keys are Airtable table names (matching schema.json),
# values are filterByFormula expressions.
HIPAA_FILTERS: dict[str, str] = {
    # HIPAA-bearing tables (ADR 0020).
    #
    # `NOT({field})` works for direct checkboxes (Accounts.HIPAA) but
    # over-filters when the field is a lookup of an unchecked checkbox:
    # Airtable returns `[None]` for that case, and `NOT([None])` evaluates
    # falsy → every row gets excluded. `NOT({field} = TRUE())` is robust
    # for both the [true] (HIPAA on) and [None]/[false] (HIPAA off) cases.
    "Accounts": "NOT({HIPAA} = TRUE())",
    "Contacts": "NOT({Account HIPAA} = TRUE())",
    "Contracts": "NOT({Account HIPAA} = TRUE())",
    "Projects": "NOT({Account HIPAA} = TRUE())",
    "Tasks": "NOT({Project HIPAA} = TRUE())",
    # No-cascade tables — no client linkage, so no HIPAA boundary applies.
    "Team": "TRUE()",
    "Goals": "TRUE()",
    "Goal Scores": "TRUE()",
    "Risk Profiles": "TRUE()",
    "Service Catalog": "TRUE()",
    # ADR 0039 — Captures table for the Captures Materializer. No client
    # linkage; rows are user-authored intake. Same permissive shape as Team.
    "Captures": "TRUE()",
    # Audit finding F7 (2026-05-28) — developer-workflow inbox added in
    # Airtable for orchestrator requests. Links to Projects but operator-
    # authored against internal projects; no HIPAA cascade applies.
    "Orchestrator Inbox": "TRUE()",
}


def hipaa_filter_for(table_name: str) -> str:
    """Return the HIPAA filterByFormula clause for an Airtable table.

    Raises ``KeyError`` for unknown tables — the orchestrator iterates a fixed
    list from ``schema.json`` so an unknown table name is a programmer error,
    not a runtime input.
    """
    return HIPAA_FILTERS[table_name]


# NOTE: the IS_AFTER incremental-pull helper (``last_modified_after``) and
# ``combine_clauses`` were removed 2026-06-10. The sync load is
# WRITE_TRUNCATE, so a checkpoint-based query filter would truncate the
# replica down to the delta. The HIPAA clause above is the only filter the
# sync may apply. ADR 0010 records the incremental deferral.
