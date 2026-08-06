"""BQ readers for the Evening Reflection composer (ADR 0036 §6).

Pattern mirrors ``agents/morning_brief/readers.py``: each reader takes
a ``BQQueryClient`` Protocol (so unit tests stub it without touching real
BQ) and a ``project_id``, exposes ``load() -> list[<row dataclass>]``,
and returns immutable rows. The composer turns those into prompt
sections.

Per ADR 0036 §1: HIPAA-flagged accounts are filtered upstream by the
WS-B Airtable sync's filterByFormula, so the readers never see HIPAA
data. The ``triaged_items`` reader joins through the
``account_owners_v`` materialized view which already applies the HIPAA
cascade.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfoNotFoundError, available_timezones

from .models import (
    ActiveRiskFlagSnippet,
    CompletedTaskSnippet,
    InFlightDecision,
    MorningBriefPlanSnippet,
    TriagedTodaySnippet,
    VoiceMemo,
)


class BQQueryClient(Protocol):
    """Minimal BQ surface — matches ``morning_brief.readers.BQQueryClient``."""

    def query_rows(self, sql: str) -> list[dict]:
        """Execute SQL, return rows as a list of dicts."""
        ...


# ----------------------------------------------------- tasks completed today

# Tasks.Owner is a singleCollaborator with no `_extract` annotation, so the
# replica column holds the collaborator's *email* (not the usrXXX id — that
# extraction is opt-in per ADR 0019 and only Team.User uses it). Compare it
# to the recipient email directly; joining team.user against it never matches.
_COMPLETED_TASKS_SQL = """
SELECT
  t._airtable_record_id AS task_id,
  t.task_name AS name,
  p.project_name,
  t.completed_date
FROM `{project}.airtable_replica.tasks` AS t
LEFT JOIN `{project}.airtable_replica.projects` AS p
  ON p._airtable_record_id = t.project[SAFE_OFFSET(0)]
WHERE t.status = 'Done'
  AND t.completed_date = @local_date
  AND LOWER(t.owner) = LOWER(@recipient)
ORDER BY t._airtable_last_modified DESC
LIMIT @limit
"""


class CompletedTasksTodayReader:
    """Airtable Tasks marked Done with completed_date = today, owned by recipient."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 20,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, recipient_email: str, local_date: date) -> list[CompletedTaskSnippet]:
        sql = _format_sql(
            _COMPLETED_TASKS_SQL,
            project=self._project_id,
            recipient=recipient_email,
            local_date=local_date,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            CompletedTaskSnippet(
                task_id=str(row["task_id"]),
                name=str(row["name"]),
                project_name=row.get("project_name"),
                completed_date=_coerce_date(row.get("completed_date")),
            )
            for row in rows
        ]


# ----------------------------------------------------- triaged items today

_TRIAGED_TODAY_SQL = """
SELECT
  ti.item_id,
  ti.severity,
  ti.reasoning AS summary,
  ti.source,
  ti.action_type,
  ti.source_url
FROM `{project}.agent_outputs.triaged_items` AS ti
WHERE DATE(ti.triaged_at, '{tz}') = @local_date
  AND (ti.owner_email = @recipient OR ti.owner_email IS NULL)
ORDER BY
  CASE ti.severity
    WHEN 'critical' THEN 0
    WHEN 'high' THEN 1
    WHEN 'medium' THEN 2
    WHEN 'low' THEN 3
    WHEN 'info' THEN 4
    ELSE 5
  END,
  ti.triaged_at DESC
LIMIT @limit
"""


class TriagedItemsTodayReader:
    """All Triaged Items routed today (any severity), owned by recipient.

    Differs from Morning Brief's reader in two ways: includes ALL severities
    (the morning brief filtered to critical/high/medium for noise control;
    reflection wants everything that came through), and is keyed on the
    recipient's local calendar date rather than a 24-hour rolling window.
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        timezone: str = "America/Los_Angeles",
        limit: int = 30,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._tz = timezone
        self._limit = limit

    def load(self, recipient_email: str, local_date: date) -> list[TriagedTodaySnippet]:
        sql = _format_sql(
            _TRIAGED_TODAY_SQL,
            project=self._project_id,
            tz=self._tz,
            recipient=recipient_email,
            local_date=local_date,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            TriagedTodaySnippet(
                item_id=str(row["item_id"]),
                severity=str(row["severity"]),
                summary=str(row["summary"]),
                source=str(row["source"]),
                action_type=str(row.get("action_type") or ""),
                source_url=row.get("source_url"),
            )
            for row in rows
        ]


# ----------------------------------------------------- today's morning brief

_MORNING_BRIEF_SQL = """
SELECT
  brief_id,
  body_markdown,
  sections_used
FROM `{project}.agent_outputs.morning_briefs`
WHERE recipient_email = @recipient
  AND local_date = @local_date
  AND success = TRUE
ORDER BY generated_at DESC
LIMIT 1
"""


class MorningBriefForTodayReader:
    """Today's morning brief for the recipient, surfaced for plan-vs-execution.

    Returns ``None`` when no brief exists for today (first day on the
    system, or morning brief failed). The composer treats ``None`` as
    "(none)" — same as an empty list reader.
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id

    def load(self, recipient_email: str, local_date: date) -> MorningBriefPlanSnippet | None:
        sql = _format_sql(
            _MORNING_BRIEF_SQL,
            project=self._project_id,
            recipient=recipient_email,
            local_date=local_date,
        )
        rows = self._bq.query_rows(sql)
        if not rows:
            return None
        row = rows[0]
        sections = tuple(row.get("sections_used") or [])
        return MorningBriefPlanSnippet(
            brief_id=str(row["brief_id"]),
            body_markdown=str(row.get("body_markdown") or ""),
            sections_used=sections,
        )


# ----------------------------------------------------- active risk flags

_ACTIVE_RISK_FLAGS_SQL = """
SELECT
  rf.flag_id,
  rf.severity,
  rf.pattern_name,
  rf.reasoning,
  ao.account_name
FROM `{project}.agent_outputs.risk_flags` AS rf
LEFT JOIN `{project}.airtable_replica.account_owners_v` AS ao
  ON ao.account_id = rf.account_id
WHERE DATE(rf.flagged_at, '{tz}') = @local_date
  AND rf.resolved_at IS NULL
  AND ao.owner_email = @recipient
  AND ao.hipaa = FALSE
ORDER BY rf.flagged_at DESC
LIMIT @limit
"""


class ActiveRiskFlagsTodayReader:
    """Risk Watcher flags from today (any severity), not yet resolved.

    Filters on ``resolved_at IS NULL`` so manually-retired flags don't
    pollute the reflection (mirrors ADR 0035 §5 routing-side filter).
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        timezone: str = "America/Los_Angeles",
        limit: int = 10,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._tz = timezone
        self._limit = limit

    def load(self, recipient_email: str, local_date: date) -> list[ActiveRiskFlagSnippet]:
        sql = _format_sql(
            _ACTIVE_RISK_FLAGS_SQL,
            project=self._project_id,
            tz=self._tz,
            recipient=recipient_email,
            local_date=local_date,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            ActiveRiskFlagSnippet(
                flag_id=str(row["flag_id"]),
                severity=str(row["severity"]),
                pattern_name=str(row["pattern_name"]),
                account_name=row.get("account_name"),
                reasoning=row.get("reasoning"),
            )
            for row in rows
        ]


# ----------------------------------------------------- recent voice memos

# ADR 0040 §2: 24h rolling window, NOT DATE(ingested_at, tz)=local_date —
# catches memos that land in the last seven minutes before the 21:00 PT
# tick and is robust to ±1h cron drift.
# ADR 0040 §3: extraction_method='gemini-2.5-flash-audio' is the cleanest
# discriminator for voice memos (every captures-path note is note_kind='inbox').
_RECENT_VOICE_MEMOS_SQL = """
SELECT
  note_id,
  markdown_content,
  ingested_at
FROM `{project}.agent_outputs.notes`
WHERE extraction_method = 'gemini-2.5-flash-audio'
  AND ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND hipaa_isolated = FALSE
ORDER BY ingested_at DESC
LIMIT @limit
"""


class RecentVoiceMemosReader:
    """Voice memos transcribed in the last 24h (ADR 0040 §2/§3).

    Notes Ingestor (ADR 0031) writes voice memos with
    ``extraction_method='gemini-2.5-flash-audio'``; this reader is the
    REFLECT-mode source. Filters HIPAA notes — all reflection readers
    must, per ADR 0006 + ADR 0031.

    Voice memos are personal: there is no ``recipient_email`` filter
    (the corpus is single-user in v1).
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 20,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self) -> list[VoiceMemo]:
        sql = _format_sql(
            _RECENT_VOICE_MEMOS_SQL,
            project=self._project_id,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            VoiceMemo(
                note_id=str(row["note_id"]),
                markdown_content=str(row.get("markdown_content") or ""),
                ingested_at=_coerce_datetime(row.get("ingested_at")),
            )
            for row in rows
        ]


# ----------------------------------------------------- in-flight decisions

# ADR 0040 §4: Active-goals proxy. Reads ``agent_outputs.decisions`` rows
# in draft/pending status from the last 30 days. The decisions table has
# no per-recipient owner column today — single-user v1.
_IN_FLIGHT_DECISIONS_SQL = """
SELECT
  decision_id,
  title,
  context,
  status,
  decided_at
FROM `{project}.agent_outputs.decisions`
WHERE status IN ('draft', 'pending')
  AND decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
ORDER BY decided_at DESC
LIMIT @limit
"""


class InFlightDecisionsReader:
    """Decisions with status draft/pending from the last 30d (ADR 0040 §4).

    Surfaced in PROMPT mode as the active-goals proxy. A future ADR may
    lift a real ``GoalsReader`` over ``airtable_replica.goals`` — for
    now decisions-pending fills the role.
    """

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 10,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self) -> list[InFlightDecision]:
        sql = _format_sql(
            _IN_FLIGHT_DECISIONS_SQL,
            project=self._project_id,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            InFlightDecision(
                decision_id=str(row["decision_id"]),
                title=str(row.get("title") or ""),
                context=row.get("context"),
                status=str(row["status"]),
                decided_at=_coerce_datetime(row.get("decided_at")),
            )
            for row in rows
        ]


# ----------------------------------------------------- helpers


def _validate_timezone(tz: str) -> str:
    """Return ``tz`` if it's a canonical IANA zone name, else raise.

    Guards the only un-parameterized interpolation in these readers. Raising
    here is correct: a misconfigured REFLECTION_TIMEZONE should fail loud at
    query-build time, not silently inject into SQL or vanish into an empty
    reflection section.
    """
    if tz not in available_timezones():
        raise ZoneInfoNotFoundError(f"REFLECTION_TIMEZONE {tz!r} is not a valid IANA timezone")
    return tz


def _format_sql(template: str, **kwargs: object) -> str:
    """Render a parameterized SQL template with literal values.

    Mirrors ``morning_brief.readers._format_sql``. The readers' callers
    pass scalar values (recipient email, limit, local date, tz string) so
    we inline them via .replace() rather than threading a parameterized
    QueryJobConfig through the BQQueryClient Protocol. SQL injection
    risk is low because all callers are internal.
    """
    rendered = template.replace("{project}", str(kwargs["project"]))
    if "tz" in kwargs:
        # tz is interpolated raw into DATE(col, '{tz}'). It comes from the
        # REFLECTION_TIMEZONE env var, so validate against the IANA database
        # before use: a bad value would otherwise inject into SQL (or, more
        # likely, make BQ raise and _safe_load() swallow it into a silently
        # empty section). Reject anything not a canonical zone name.
        rendered = rendered.replace("{tz}", _validate_timezone(str(kwargs["tz"])))
    if "recipient" in kwargs:
        recipient = str(kwargs["recipient"]).replace("'", "''")
        rendered = rendered.replace("@recipient", f"'{recipient}'")
    if "local_date" in kwargs:
        local_date = kwargs["local_date"]
        if isinstance(local_date, date):
            local_date_str = local_date.isoformat()
        else:
            local_date_str = str(local_date).replace("'", "''")
        rendered = rendered.replace("@local_date", f"DATE '{local_date_str}'")
    if "limit" in kwargs:
        rendered = rendered.replace("@limit", str(int(kwargs["limit"])))
    return rendered


def _coerce_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _coerce_datetime(raw: object) -> datetime:
    """Coerce a BQ TIMESTAMP value (datetime / ISO string / epoch-ish) to UTC.

    Used by ``RecentVoiceMemosReader`` and ``InFlightDecisionsReader`` —
    both surface ``ingested_at`` / ``decided_at`` as UTC datetimes
    downstream. Naive datetimes are assumed UTC. Unparseable values
    default to ``datetime.now(timezone.utc)`` so a malformed row doesn't
    take down the whole reader.
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(tz=UTC)
