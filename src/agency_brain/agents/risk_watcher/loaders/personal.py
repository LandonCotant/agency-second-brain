"""Personal-segment client-state loader (ADR 0042).

Builds one ``ClientState`` per Contact with a non-null
``Relationship Type``. Personal contacts often have no Account link;
the HIPAA cascade still functions because the sync's
``NOT({Account HIPAA} = TRUE())`` filter handles null lookups
correctly (per ``sync/hipaa_filters.py``).

Per-relationship-type recency thresholds are evaluated by
``PersonalReEngagementSignal``; this loader pre-computes
``days_since_contact`` + ``days_since_created`` BQ-side so the
signal does pure integer arithmetic.
"""

from __future__ import annotations

from ..models import ClientState, Segment
from .common import BQQueryClient


class PersonalClientStateLoader:
    """Build one ``ClientState`` per personal Contact.

    Gate: ``relationship_type IS NOT NULL`` excludes work-only
    contacts (the existing client segments cover those via Account
    links). HIPAA filtering is upstream at the replica sync (PRD
    §4.1 layer 2).

    ``extras`` populated:
    - ``relationship_type`` (str): drives threshold selection.
    - ``warmth`` (str | None): drives severity escalation.
    - ``last_contact_at`` (date | None): raw value, surfaced for
      audit/debug. The signal uses ``days_since_contact`` instead.
    - ``days_since_contact`` (int | None): pre-computed via
      ``DATE_DIFF(CURRENT_DATE(), last_contact, DAY)``; None when
      ``last_contact IS NULL``.
    - ``days_since_created`` (int | None): recency proxy when
      ``last_contact`` is null. Always non-null in practice
      (createdTime is system-set by Airtable).
    """

    def __init__(self, *, bq: BQQueryClient, project_id: str) -> None:
        self._bq = bq
        self._project_id = project_id

    def load(self) -> tuple[ClientState, ...]:
        sql = (
            "SELECT _airtable_record_id AS contact_id, "  # noqa: S608  project_id is config; no user input
            "       name AS contact_name, "
            "       relationship_type, "
            "       warmth, "
            "       last_contact, "
            "       DATE_DIFF(CURRENT_DATE(), last_contact, DAY) "
            "         AS days_since_contact, "
            "       DATE_DIFF(CURRENT_DATE(), DATE(_airtable_last_modified), DAY) "
            "         AS days_since_created "
            f"FROM `{self._project_id}.airtable_replica.contacts` "
            "WHERE relationship_type IS NOT NULL"
        )
        rows = self._bq.query_rows(sql)

        states: list[ClientState] = []
        for r in rows:
            contact_id = r["contact_id"]
            states.append(
                ClientState(
                    account_id=contact_id,
                    account_name=r.get("contact_name") or "(unnamed contact)",
                    segment=Segment.PERSONAL,
                    project_id=None,
                    aspects=(),
                    baseline={},
                    extras={
                        "relationship_type": r["relationship_type"],
                        "warmth": r.get("warmth"),
                        "last_contact_at": r.get("last_contact"),
                        "days_since_contact": r.get("days_since_contact"),
                        "days_since_created": r.get("days_since_created"),
                    },
                )
            )
        return tuple(states)
