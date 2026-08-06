"""BQ enricher for People Sync AUTO sections (ADR 0057 §5).

Each Account note has three AUTO sections that this module populates
from BQ:

  - **Active engagements**: ``airtable_replica.projects`` rows where
    the account is the parent and ``status`` is open (Not Started /
    Active / Blocked).
  - **Recent activity** (last 30d): UNION of triaged_items mentioning
    the account name + calendar_events with attendees on the account's
    contacts.
  - **Open risks**: ``agent_outputs.risk_flags`` where ``account_name =
    X`` and not resolved.

Each section function returns a markdown string starting with the
``AUTO_MARKER`` followed by a bulleted list. ``"\\n"`` is the trailing
character so the section composes cleanly.

Phase 4 will add the contact-side ``Conversation log`` enricher here
(commented out for now).
"""

from __future__ import annotations

import logging
from typing import Protocol

from .sections import AUTO_MARKER

log = logging.getLogger("agency_brain.agents.people_sync.bq_enricher")


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


# --------------------------------------------------------------- Account sections


_ACTIVE_ENGAGEMENTS_SQL = """\
SELECT p.project_name, p.status, p.health, p.target_end_date
FROM `{project_id}.airtable_replica.projects` p
WHERE @account_id IN UNNEST(p.account)
  AND p.status IN ('Not Started', 'Active', 'Blocked')
  AND COALESCE(p.hipaa_excluded, FALSE) = FALSE
ORDER BY p.status, p.project_name
LIMIT 25
"""


# UNION of two sources, both clipped to the last 30 days. ``note_hits``
# surfaces any note (calendar event, email, capture, win, decision, area
# — but NOT ``galaxy``, which would self-reference) whose
# ``markdown_content`` mentions the account name. ``risk_hits`` collapses
# repeat-daily risk firings into one line per (pattern, severity) so a
# 9-day-running Owner Disengagement flag shows up once with a count,
# not nine separate bullets. ``triaged_items`` is still excluded — its
# schema doesn't carry the columns needed for account attribution
# (revisit when there's a corpus row per inbound email).
_RECENT_ACTIVITY_SQL = """\
WITH note_hits AS (
  SELECT
    DATE(n.created_at) AS activity_date,
    n.note_kind AS source,
    SUBSTR(n.filename, 0, 80) AS line,
    n.created_at AS sort_ts
  FROM `{project_id}.agent_outputs.notes` n
  WHERE n.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
    AND LOWER(n.markdown_content) LIKE LOWER(@account_name_like)
    AND COALESCE(n.hipaa_isolated, FALSE) = FALSE
    AND n.note_kind IN ('calendar_event', 'email', 'capture', 'win', 'decision', 'area')
),
risk_hits AS (
  SELECT
    DATE(MAX(r.flagged_at)) AS activity_date,
    'risk_flag' AS source,
    CONCAT(
      UPPER(r.severity), ': ', r.pattern_name,
      ' — ', CAST(COUNT(*) AS STRING), ' firings since ',
      CAST(DATE(MIN(r.flagged_at)) AS STRING)
    ) AS line,
    MAX(r.flagged_at) AS sort_ts
  FROM `{project_id}.agent_outputs.risk_flags` r
  WHERE r.account_id = @account_id
    AND r.flagged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY r.pattern_name, r.severity
)
SELECT activity_date, source, line FROM (
  SELECT activity_date, source, line, sort_ts FROM note_hits
  UNION ALL
  SELECT activity_date, source, line, sort_ts FROM risk_hits
)
ORDER BY sort_ts DESC, source, line
LIMIT 15
"""


# Group by (pattern, severity) so a daily-firing risk renders as one
# bullet with a count + date range rather than N near-identical lines.
_OPEN_RISKS_SQL = """\
SELECT
  r.pattern_name,
  r.severity,
  MAX(r.flagged_at) AS last_flagged_at,
  MIN(r.flagged_at) AS first_flagged_at,
  COUNT(*) AS firing_count,
  ANY_VALUE(r.signal_evidence) AS signal_evidence
FROM `{project_id}.agent_outputs.risk_flags` r
WHERE r.account_id = @account_id
  AND r.resolved_at IS NULL
GROUP BY r.pattern_name, r.severity
ORDER BY r.severity DESC, last_flagged_at DESC
LIMIT 10
"""


# --------------------------------------------------------------- Contact sections (Phase 4)


_CONVERSATION_LOG_SQL = """\
-- v1: triage_hits omitted. agent_outputs.triaged_items doesn't carry
-- received_at / subject / from_email columns (it's a structured-action
-- table, not raw email content). Revisit when there's a corpus row
-- per inbound email.
WITH calendar_hits AS (
  SELECT
    DATE(SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', n.event_metadata.start)) AS log_date,
    CONCAT('meeting: ', SUBSTR(n.filename, 0, 80)) AS line,
    'calendar' AS source
  FROM `{project_id}.agent_outputs.notes` n
  WHERE n.note_kind = 'calendar_event'
    AND n.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
    AND @email != ''
    AND EXISTS (
      SELECT 1 FROM UNNEST(n.event_metadata.attendees) AS att
      WHERE LOWER(att) = LOWER(@email)
    )
),
wikilink_hits AS (
  SELECT
    DATE(n.created_at) AS log_date,
    CONCAT('mention: ', SUBSTR(n.filename, 0, 80)) AS line,
    'wikilink' AS source
  FROM `{project_id}.agent_outputs.notes` n
  WHERE n.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
    AND n.note_kind IN ('capture', 'decision', 'win', 'galaxy', 'area')
    AND LOWER(n.markdown_content) LIKE LOWER(CONCAT('%[[', @contact_name, ']]%'))
)
SELECT * FROM (
  SELECT * FROM calendar_hits
  UNION ALL
  SELECT * FROM wikilink_hits
)
WHERE log_date IS NOT NULL
ORDER BY log_date DESC, source, line
LIMIT 15
"""


class ContactEnricher:
    """Builds the ``## Conversation log`` AUTO section for a Contact note.

    Unions three sources over the last 90 days:
      - ``triaged_items`` from the contact's email address
      - ``notes`` (calendar_event kind) where the contact is an attendee
      - ``notes`` (capture/decision/win/galaxy/area kinds) whose
        ``markdown_content`` contains the wikilink ``[[Contact Name]]``

    Returns markdown grouped by date with one bullet per source.
    """

    def __init__(self, *, bq_query: BQQueryClient, project_id: str) -> None:
        self._bq = bq_query
        self._project_id = project_id

    def conversation_log(self, *, contact_name: str, email: str | None) -> str:
        """Returns the AUTO-section body for ## Conversation log."""
        sql = _CONVERSATION_LOG_SQL.format(project_id=self._project_id)
        try:
            rows = self._bq.query_rows(
                sql,
                parameters=[
                    {"name": "contact_name", "type": "STRING", "value": contact_name},
                    {"name": "email", "type": "STRING", "value": email or ""},
                ],
            )
        except Exception:
            log.exception("people_sync.enricher.conversation_log_failed contact=%s", contact_name)
            return f"{AUTO_MARKER}\n(query failed; will retry next tick)\n"

        if not rows:
            return f"{AUTO_MARKER}\n(no conversation traces in last 90 days)\n"

        # Group by date with bullets per entry.
        lines = [AUTO_MARKER]
        current_date: str | None = None
        for r in rows:
            d = str(r.get("log_date") or "")
            if d != current_date:
                lines.append(f"\n### {d}")
                current_date = d
            line_text = str(r.get("line") or "")
            lines.append(f"- {line_text}")
        return "\n".join(lines) + "\n"


class AccountEnricher:
    """Builds the three AUTO sections for an Account note."""

    def __init__(self, *, bq_query: BQQueryClient, project_id: str) -> None:
        self._bq = bq_query
        self._project_id = project_id

    def active_engagements(self, *, airtable_id: str) -> str:
        """Returns the AUTO-section body for ## Active engagements."""
        sql = _ACTIVE_ENGAGEMENTS_SQL.format(project_id=self._project_id)
        try:
            rows = self._bq.query_rows(
                sql, parameters=[{"name": "account_id", "type": "STRING", "value": airtable_id}]
            )
        except Exception:
            log.exception("people_sync.enricher.active_engagements_failed account=%s", airtable_id)
            return f"{AUTO_MARKER}\n(query failed; will retry next tick)\n"

        if not rows:
            return f"{AUTO_MARKER}\n(no active engagements)\n"

        lines = [AUTO_MARKER]
        for r in rows:
            name = str(r.get("project_name") or "?")
            status = str(r.get("status") or "?")
            health = r.get("health")
            end = r.get("target_end_date")
            health_suffix = f" ({health} health)" if health else ""
            end_suffix = f" — ends {end}" if end else ""
            lines.append(f"- [[{name}]] — {status}{health_suffix}{end_suffix}")
        return "\n".join(lines) + "\n"

    def recent_activity(self, *, account_name: str, airtable_id: str) -> str:
        """Returns the AUTO-section body for ## Recent activity.

        UNIONs note mentions (last 30d, all kinds except galaxy) with
        risk-flag firings (collapsed by pattern + severity). ``airtable_id``
        is needed to filter ``risk_flags.account_id``; ``account_name`` is
        the LIKE pattern over ``notes.markdown_content``.
        """
        sql = _RECENT_ACTIVITY_SQL.format(project_id=self._project_id)
        like_pattern = f"%{account_name}%"
        try:
            rows = self._bq.query_rows(
                sql,
                parameters=[
                    {"name": "account_name_like", "type": "STRING", "value": like_pattern},
                    {"name": "account_id", "type": "STRING", "value": airtable_id},
                ],
            )
        except Exception:
            log.exception("people_sync.enricher.recent_activity_failed account=%s", account_name)
            return f"{AUTO_MARKER}\n(query failed; will retry next tick)\n"

        if not rows:
            return f"{AUTO_MARKER}\n(no recent activity in last 30 days)\n"

        lines = [AUTO_MARKER]
        for r in rows:
            d = r.get("activity_date") or ""
            line_text = str(r.get("line") or "")
            lines.append(f"- {d} — {line_text}")
        return "\n".join(lines) + "\n"

    def open_risks(self, *, airtable_id: str) -> str:
        """Returns the AUTO-section body for ## Open risks.

        Collapses repeat firings of the same (pattern, severity) into one
        bullet with a firing-count + first/last date range. A 9-day daily
        Owner Disengagement flag renders as one line, not nine.

        Filters ``agent_outputs.risk_flags`` by ``account_id`` (the
        Airtable record id, NOT the company name — risk_flags stores
        the FK, not the display name).
        """
        sql = _OPEN_RISKS_SQL.format(project_id=self._project_id)
        try:
            rows = self._bq.query_rows(
                sql, parameters=[{"name": "account_id", "type": "STRING", "value": airtable_id}]
            )
        except Exception:
            log.exception("people_sync.enricher.open_risks_failed account=%s", airtable_id)
            return f"{AUTO_MARKER}\n(query failed; will retry next tick)\n"

        if not rows:
            return f"{AUTO_MARKER}\n(no open risks)\n"

        lines = [AUTO_MARKER]
        for r in rows:
            pattern = str(r.get("pattern_name") or "?")
            severity = str(r.get("severity") or "?")
            count = int(r.get("firing_count") or 1)
            first = r.get("first_flagged_at")
            last = r.get("last_flagged_at")
            ev = str(r.get("signal_evidence") or "").strip()
            ev_short = ev[:100] + "…" if len(ev) > 100 else ev
            if count > 1:
                # Format dates as YYYY-MM-DD only (drop time component).
                first_date = str(first).split(" ")[0].split("T")[0] if first else ""
                last_date = str(last).split(" ")[0].split("T")[0] if last else ""
                summary = f"{count} firings, {first_date} → {last_date}"
            else:
                flagged = str(last or "").split(".")[0]  # strip subsec
                summary = f"flagged {flagged}"
            lines.append(f"- **{severity}** {pattern} ({summary}): {ev_short}")
        return "\n".join(lines) + "\n"
