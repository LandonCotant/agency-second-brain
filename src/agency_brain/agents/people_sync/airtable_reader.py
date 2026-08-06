"""BigQuery reader for the People Sync agent (ADR 0057).

Reads ``airtable_replica.accounts`` and ``airtable_replica.contacts``
with the HIPAA cascade enforced in SQL:

  - Accounts with ``hipaa = true`` are excluded.
  - Contacts whose primary account has ``hipaa = true`` are excluded.
  - Both tables also respect ``hipaa_excluded`` set by the sync pipeline.

The Contacts query joins ``account[OFFSET(0)]`` (Airtable record id of
the contact's primary account) against ``accounts._airtable_record_id``
to derive ``organization`` (= ``accounts.company_name``) and apply the
cascade. Contacts with no account land with ``organization=None`` and
pass the cascade filter (no parent to be HIPAA).
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Protocol

from .models import AccountRow, ContactRow

log = logging.getLogger("agency_brain.agents.people_sync.airtable_reader")


class BQQueryClient(Protocol):
    """Minimal BQ surface the reader uses. Matches the
    ``_ParameterizedBQAdapter`` pattern in ``librarian/main.py``."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


_ACCOUNTS_SQL = """\
SELECT
  _airtable_record_id AS airtable_id,
  company_name AS name,
  status,
  industry_ai AS industry,
  account_manager,
  google_drive_folder AS drive_folder_url,
  notes
FROM `{project_id}.airtable_replica.accounts`
WHERE COALESCE(hipaa, FALSE) = FALSE
  AND COALESCE(hipaa_excluded, FALSE) = FALSE
  AND company_name IS NOT NULL
  AND TRIM(company_name) != ''
ORDER BY company_name
"""


_CONTACTS_SQL = """\
WITH non_hipaa_accounts AS (
  SELECT _airtable_record_id AS account_id, company_name
  FROM `{project_id}.airtable_replica.accounts`
  WHERE COALESCE(hipaa, FALSE) = FALSE
    AND COALESCE(hipaa_excluded, FALSE) = FALSE
),
contacts_with_primary AS (
  SELECT
    c._airtable_record_id AS airtable_id,
    c.name,
    c.email,
    c.role,
    -- Primary account = first element of the array. NULL for
    -- unaffiliated contacts. ``[SAFE_OFFSET(0)]`` returns NULL on
    -- empty array instead of erroring (BQ array indexing syntax).
    c.account[SAFE_OFFSET(0)] AS primary_account_id,
    c.relationship_type,
    c.warmth,
    c.last_contact,
    c.next_followup,
    c.linkedin_url AS linkedin,
    c.phone,
    c.notes,
    c.hipaa_excluded
  FROM `{project_id}.airtable_replica.contacts` c
)
SELECT
  cwp.airtable_id,
  cwp.name,
  cwp.email,
  cwp.role,
  acc.company_name AS organization,
  cwp.relationship_type,
  cwp.warmth,
  cwp.last_contact,
  cwp.next_followup,
  cwp.linkedin,
  cwp.phone,
  cwp.notes
FROM contacts_with_primary cwp
LEFT JOIN non_hipaa_accounts acc ON acc.account_id = cwp.primary_account_id
WHERE COALESCE(cwp.hipaa_excluded, FALSE) = FALSE
  AND cwp.name IS NOT NULL
  AND TRIM(cwp.name) != ''
  -- HIPAA cascade: if contact HAS a primary account but it's not in
  -- non_hipaa_accounts, the LEFT JOIN returns NULL on acc.company_name
  -- AND we know primary_account_id was set → exclude. If
  -- primary_account_id IS NULL (unaffiliated), accept (no parent).
  AND (
    cwp.primary_account_id IS NULL
    OR acc.account_id IS NOT NULL
  )
ORDER BY cwp.name
"""


class AirtableReader:
    """Reads accounts + contacts from the Airtable BQ replica."""

    def __init__(self, *, bq_query: BQQueryClient, project_id: str) -> None:
        self._bq = bq_query
        self._project_id = project_id

    def list_accounts(self) -> list[AccountRow]:
        sql = _ACCOUNTS_SQL.format(project_id=self._project_id)
        rows = self._bq.query_rows(sql)
        out: list[AccountRow] = []
        for r in rows:
            out.append(
                AccountRow(
                    airtable_id=str(r.get("airtable_id") or ""),
                    name=str(r.get("name") or "").strip(),
                    status=_str_or_none(r.get("status")),
                    industry=_str_or_none(r.get("industry")),
                    account_manager=_str_or_none(r.get("account_manager")),
                    drive_folder_url=_str_or_none(r.get("drive_folder_url")),
                    notes=_str_or_none(r.get("notes")),
                )
            )
        log.info("people_sync.reader.accounts_listed count=%d", len(out))
        return out

    def list_contacts(self) -> list[ContactRow]:
        sql = _CONTACTS_SQL.format(project_id=self._project_id)
        rows = self._bq.query_rows(sql)
        out: list[ContactRow] = []
        for r in rows:
            out.append(
                ContactRow(
                    airtable_id=str(r.get("airtable_id") or ""),
                    name=str(r.get("name") or "").strip(),
                    email=_str_or_none(r.get("email")),
                    role=_str_or_none(r.get("role")),
                    organization=_str_or_none(r.get("organization")),
                    relationship_type=_str_or_none(r.get("relationship_type")),
                    warmth=_str_or_none(r.get("warmth")),
                    last_contact=_date_or_none(r.get("last_contact")),
                    next_followup=_date_or_none(r.get("next_followup")),
                    linkedin=_str_or_none(r.get("linkedin")),
                    phone=_str_or_none(r.get("phone")),
                    notes=_str_or_none(r.get("notes")),
                )
            )
        log.info("people_sync.reader.contacts_listed count=%d", len(out))
        return out


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date_or_none(v) -> date_cls | None:
    if v is None:
        return None
    if isinstance(v, date_cls):
        return v
    # BQ returns dates as 'YYYY-MM-DD' strings via the
    # _ParameterizedBQAdapter.
    s = str(v).strip()
    if not s:
        return None
    try:
        return date_cls.fromisoformat(s[:10])
    except ValueError:
        log.warning("people_sync.reader.bad_date value=%r", v)
        return None
