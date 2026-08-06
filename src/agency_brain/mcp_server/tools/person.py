"""Person-related MCP tools (ADR 0057 §6).

Two tools:

  - ``person_summary(name_or_email)``: structured cross-source briefing on
    a contact. Resolves Airtable Contacts → Brain Drive note → recent
    activity from BQ in one call. Use it from routines + ad-hoc to
    front-load "who is this" before a meeting / cold-outreach / weekly-
    review followup.

  - ``sync_people()``: manually triggers ``asb-people-sync`` Cloud Run Job.
    The job otherwise runs Sundays 06:15 UTC; this is the "I just edited
    Airtable, refresh now" escape hatch.

Both tools require the local Brain MCP server (which already has BQ
access via the same ADC the user authed in earlier this session).
``sync_people`` additionally requires ``gcloud`` on PATH with
run.developer or run.invoker on the Job.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from ...common.bq_helpers import exclude_hipaa, filter_recency
from ..clients import get_config, query_rows

log = logging.getLogger("agency_brain.mcp_server.tools.person")


# --------------------------------------------------------------- person_summary


_RESOLVE_SQL = f"""\
SELECT
  c._airtable_record_id AS airtable_id,
  c.name,
  c.email,
  c.role,
  c.relationship_type,
  c.warmth,
  c.last_contact,
  c.next_followup,
  c.linkedin_url AS linkedin,
  c.phone,
  c.notes,
  c.account[SAFE_OFFSET(0)] AS primary_account_id
FROM `{{project_id}}.airtable_replica.contacts` c
WHERE {exclude_hipaa('c')}
  AND (
    LOWER(c.name) = LOWER(@query_str)
    OR LOWER(c.email) = LOWER(@query_str)
  )
ORDER BY c._airtable_last_modified DESC NULLS LAST
LIMIT 5
"""


# Pre-bq_helpers, this filter used the raw ``a.hipaa`` checkbox column,
# which diverged from how every other code path filters accounts (they
# all use the cascade-derived ``a.hipaa_excluded``). The helper enforces
# the correct column. Audit note 2026-05-19.
_ORG_SQL = f"""\
SELECT a.company_name
FROM `{{project_id}}.airtable_replica.accounts` a
WHERE a._airtable_record_id = @account_id
  AND {exclude_hipaa('a')}
LIMIT 1
"""


_RECENT_TRIAGE_SQL = f"""\
-- v1: pulls calendar events where the contact is an attendee + wikilink
-- mentions in recent notes. agent_outputs.triaged_items doesn't carry
-- received_at / subject / from_email columns (structured-action table,
-- not raw email content) so triage-by-email lookups aren't possible
-- against that table yet. Revisit when there's a corpus row per
-- inbound email.
WITH calendar_hits AS (
  SELECT
    DATE(SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', n.event_metadata.start)) AS activity_date,
    CONCAT('meeting: ', SUBSTR(n.filename, 0, 80)) AS line,
    n.event_metadata.organizer AS source_detail
  FROM `{{project_id}}.agent_outputs.notes` n
  WHERE n.note_kind = 'calendar_event'
    AND {filter_recency('n.created_at', 90)}
    AND @email != ''
    AND EXISTS (
      SELECT 1 FROM UNNEST(n.event_metadata.attendees) AS att
      WHERE LOWER(att) = LOWER(@email)
    )
)
SELECT * FROM calendar_hits
ORDER BY activity_date DESC
LIMIT 5
"""


def person_summary(name_or_email: str) -> dict[str, Any]:
    """Structured briefing on a person across Airtable + Brain.

    USE THIS WHEN the user asks:
      - "Tell me about <person>" / "Who is <person>"
      - "Brief me on <person>" before a meeting
      - "What do I know about <person>"
      - Anything involving a specific named human (NOT a client/company —
        for company-level queries use ``client_summary``)

    Resolves Airtable Contacts by exact case-insensitive match on
    ``name`` OR ``email`` (whichever matches the query). On multiple
    matches (e.g. two contacts named "Sam Q"), returns the most
    recently modified.

    Args:
        name_or_email: A person's name OR their email address. Both
            paths return the same shape.

    Returns:
        ``{
            "found": bool,
            "name": str | None,
            "email": str | None,
            "role": str | None,
            "organization": str | None,
            "relationship_type": str | None,
            "warmth": str | None,
            "last_contact": str | None,    # ISO date
            "next_followup": str | None,
            "linkedin": str | None,
            "phone": str | None,
            "notes": str | None,
            "open_followup_due": bool,     # next_followup <= today
            "recent_activity": [
                {"date": str, "line": str}
            ],
        }``

    Returns ``{"found": false}`` when no Airtable contact matches.
    HIPAA-flagged contacts (or contacts whose primary account is HIPAA)
    are excluded; this tool will return ``found: false`` for them.
    """
    cfg = get_config()
    q = (name_or_email or "").strip()
    if not q:
        return {"found": False}

    contacts = query_rows(
        _RESOLVE_SQL.format(project_id=cfg.project_id),
        parameters=[{"name": "query_str", "type": "STRING", "value": q}],
    )
    if not contacts:
        return {"found": False}

    c = contacts[0]
    organization: str | None = None
    if c.get("primary_account_id"):
        rows = query_rows(
            _ORG_SQL.format(project_id=cfg.project_id),
            parameters=[
                {"name": "account_id", "type": "STRING", "value": str(c["primary_account_id"])}
            ],
        )
        if rows:
            organization = str(rows[0].get("company_name") or "") or None

    recent_activity: list[dict[str, Any]] = []
    email = c.get("email")
    if email:
        for r in query_rows(
            _RECENT_TRIAGE_SQL.format(project_id=cfg.project_id),
            parameters=[{"name": "email", "type": "STRING", "value": str(email)}],
        ):
            recent_activity.append(
                {
                    "date": str(r.get("activity_date") or ""),
                    "line": str(r.get("line") or ""),
                }
            )

    from datetime import date as date_cls

    next_followup = c.get("next_followup")
    open_followup_due = False
    if next_followup:
        try:
            nf = (
                next_followup
                if isinstance(next_followup, date_cls)
                else date_cls.fromisoformat(str(next_followup)[:10])
            )
            open_followup_due = nf <= date_cls.today()
        except ValueError:
            open_followup_due = False

    return {
        "found": True,
        "name": _str_or_none(c.get("name")),
        "email": _str_or_none(c.get("email")),
        "role": _str_or_none(c.get("role")),
        "organization": organization,
        "relationship_type": _str_or_none(c.get("relationship_type")),
        "warmth": _str_or_none(c.get("warmth")),
        "last_contact": _date_str_or_none(c.get("last_contact")),
        "next_followup": _date_str_or_none(c.get("next_followup")),
        "linkedin": _str_or_none(c.get("linkedin")),
        "phone": _str_or_none(c.get("phone")),
        "notes": _str_or_none(c.get("notes")),
        "open_followup_due": open_followup_due,
        "recent_activity": recent_activity,
    }


# --------------------------------------------------------------- pending_followups


_PENDING_FOLLOWUPS_SQL = f"""\
SELECT
  c._airtable_record_id AS contact_id,
  c.name,
  c.email,
  c.warmth,
  c.relationship_type,
  c.last_contact,
  c.next_followup,
  c.account[SAFE_OFFSET(0)] AS primary_account_id
FROM `{{project_id}}.airtable_replica.contacts` c
WHERE c.next_followup IS NOT NULL
  AND c.next_followup <= DATE_ADD(CURRENT_DATE(), INTERVAL @window_days DAY)
  AND {exclude_hipaa('c')}
ORDER BY c.next_followup ASC
LIMIT @limit
"""


def pending_followups(window_days: int = 0, limit: int = 20) -> dict[str, Any]:
    """List Airtable Contacts with ``next_followup`` due (or overdue).

    USE THIS WHEN the user asks:
      - "Who am I behind on?" / "Who do I need to follow up with?"
      - "Anyone overdue?" / "Who needs a check-in?"
      - "What's on my followup list this week?" (pass ``window_days=7``)

    DO NOT USE FOR:
      - General "tell me about <person>" — use ``person_summary``.
      - Open account risks — use ``open_risk_flags`` (a different surface;
        risks are pattern-based, followups are operator-scheduled).
      - Adding/scheduling new followups — Airtable is canonical for those.

    Source: ``airtable_replica.contacts`` filtered by
    ``next_followup <= today + window_days``. HIPAA-excluded rows are
    filtered. Sorted by ``next_followup`` ascending (most-overdue first).

    Args:
        window_days: Look-ahead. ``0`` (default) = due today or overdue.
            ``7`` = due in the next week or overdue. Clamped to [0, 60].
        limit: Max rows to return. Default 20, clamped to [1, 100].

    Returns:
        ``{
            "contacts": [
                {
                    "contact_id": str,            # Airtable _airtable_record_id
                    "name": str,
                    "email": str | None,
                    "warmth": str | None,         # hot/warm/cool/cold/new
                    "relationship_type": str | None,
                    "last_contact": str | None,   # ISO YYYY-MM-DD
                    "next_followup": str | None,  # ISO YYYY-MM-DD
                    "primary_account_id": str | None,
                }
            ],
            "total": int,
            "window_days": int,                   # echo of the parameter
        }``
    """
    cfg = get_config()
    safe_window = max(0, min(int(window_days), 60))
    safe_limit = max(1, min(int(limit), 100))
    sql = _PENDING_FOLLOWUPS_SQL.format(project_id=cfg.project_id)
    rows = query_rows(
        sql,
        parameters=[
            {"name": "window_days", "type": "INT64", "value": safe_window},
            {"name": "limit", "type": "INT64", "value": safe_limit},
        ],
    )
    return {
        "contacts": [
            {
                "contact_id": r.get("contact_id"),
                "name": r.get("name"),
                "email": r.get("email"),
                "warmth": r.get("warmth"),
                "relationship_type": r.get("relationship_type"),
                "last_contact": _date_str_or_none(r.get("last_contact")),
                "next_followup": _date_str_or_none(r.get("next_followup")),
                "primary_account_id": r.get("primary_account_id"),
            }
            for r in rows
        ],
        "total": len(rows),
        "window_days": safe_window,
    }


# --------------------------------------------------------------- sync_people


_DEFAULT_REGION = "us-central1"
# Region is an LLM-callable tool arg that goes into a gcloud argv. shell=False
# already blocks shell injection, but an allowlist stops a prompt-injected
# value from smuggling extra flags or pointing the call at an unexpected
# region. These are the regions this project deploys into.
_ALLOWED_REGIONS = frozenset({"us-central1", "us-east1", "us-east4", "us-west1"})


def sync_people(region: str = _DEFAULT_REGION) -> dict[str, Any]:
    """Manually fire ``asb-people-sync`` Cloud Run Job.

    USE THIS WHEN the user has just edited Airtable contacts/accounts
    and wants their Brain notes refreshed without waiting for the next
    Sunday tick. Examples:
      - "Sync the people notes"
      - "Refresh the personal CRM"
      - "Push my Airtable edits to the Brain"

    The Job otherwise runs Sundays 06:15 UTC. Shells to
    ``gcloud run jobs execute asb-people-sync --region=us-central1
    --wait``; requires ``gcloud`` on PATH and the local user's ADC to
    have run.invoker on the Job (owner@example.com owner =>
    yes).

    Args:
        region: GCP region; default ``us-central1``.

    Returns:
        ``{"succeeded": bool, "execution_name": str | None,
           "stdout_tail": str, "stderr_tail": str}``
    """
    if region not in _ALLOWED_REGIONS:
        return {
            "succeeded": False,
            "execution_name": None,
            "stdout_tail": "",
            "stderr_tail": (f"invalid region {region!r}; allowed: {sorted(_ALLOWED_REGIONS)}"),
        }
    cfg = get_config()
    cmd = [
        "gcloud",
        "run",
        "jobs",
        "execute",
        "asb-people-sync",
        f"--region={region}",
        f"--project={cfg.project_id}",
        "--wait",
        "--format=value(metadata.name)",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {
            "succeeded": False,
            "execution_name": None,
            "stdout_tail": "",
            "stderr_tail": "timeout after 900s",
        }
    except FileNotFoundError:
        return {
            "succeeded": False,
            "execution_name": None,
            "stdout_tail": "",
            "stderr_tail": "gcloud not on PATH; install Google Cloud SDK or run job manually",
        }

    stdout_tail = (proc.stdout or "").strip()[-500:]
    stderr_tail = (proc.stderr or "").strip()[-500:]
    return {
        "succeeded": proc.returncode == 0,
        "execution_name": stdout_tail if proc.returncode == 0 else None,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


# --------------------------------------------------------------- helpers


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date_str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:10]  # crude trim to ISO date
