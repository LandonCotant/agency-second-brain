"""BQ readers for the Brag Spotter composer (ADR 0043 §3 + §5).

Each reader takes a `BQQueryClient` Protocol and a `project_id`,
exposes `load() -> list[<row dataclass>]`, and returns immutable rows.
The composer turns those into prompt blocks. HIPAA filtering is at the
source per ADR 0043 §8.

Pattern mirrors `morning_brief.readers` — readers swallow nothing;
failures bubble up to the agent's per-reader try/except.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .models import (
    DecisionRow,
    ExistingWinRow,
    NoteRow,
    ReflectionRow,
    RoutedEventRow,
    TriagedItemRow,
)


class BQQueryClient(Protocol):
    """Minimal BQ surface — matches `morning_brief.readers.BQQueryClient`."""

    def query_rows(self, sql: str) -> list[dict]: ...


# ---------------------------------------------------------------- triaged items

_TRIAGED_ITEMS_SQL = """
SELECT
  ti.item_id,
  ti.triaged_at,
  ti.severity,
  ti.reasoning,
  ti.source,
  ti.source_url,
  ti.positive_goal_achieving
FROM `{project}.agent_outputs.triaged_items` AS ti
WHERE ti.triaged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  AND ti.actionable = TRUE
  AND ti.severity = 'critical'
  AND ti.positive_goal_achieving = 'strong'
ORDER BY ti.triaged_at DESC
LIMIT @limit
"""
# Narrowed 2026-05-14 — previously severity IN (critical, high, medium)
# which fed nearly every operational classification into Brag Spotter's
# candidate pool, drowning out actual wins. Now requires BOTH highest
# severity AND PGA='strong' (Triage Agent's signal that the item
# materially advances an active goal). Drops the candidate volume by
# ~95% in 2026-05-14 prod data.


class RecentTriagedItemsReader:
    """Last-N-day actionable triaged items, severity≥medium."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        lookback_days: int = 7,
        limit: int = 50,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._lookback_days = lookback_days
        self._limit = limit

    def load(self) -> list[TriagedItemRow]:
        sql = _format_sql(
            _TRIAGED_ITEMS_SQL,
            project=self._project_id,
            lookback_days=self._lookback_days,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            TriagedItemRow(
                item_id=str(row["item_id"]),
                triaged_at=row["triaged_at"],
                severity=str(row["severity"]),
                reasoning=str(row["reasoning"]),
                source=str(row["source"]),
                source_url=row.get("source_url"),
                positive_goal_achieving=row.get("positive_goal_achieving"),
            )
            for row in rows
        ]


# ---------------------------------------------------------------- routed events

_ROUTED_EVENTS_SQL = """
WITH first_dispatch AS (
  SELECT
    re.item_id,
    ANY_VALUE(re.channel) AS channel,
    MIN(re.routed_at) AS routed_at
  FROM `{project}.agent_outputs.routed_events` AS re
  WHERE re.routed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  GROUP BY re.item_id
)
SELECT item_id, channel, routed_at
FROM first_dispatch
ORDER BY routed_at DESC
LIMIT @limit
"""
# Deduped 2026-05-14 — previously emitted one row per (item_id, channel)
# fan-out, so a single email routed to chat + gmail produced 2-3 rows
# that looked like 2-3 distinct wins to the LLM. Now collapsed to one
# row per unique item_id (first dispatch only). The composer prompt
# already says routing is process noise, so this is the conservative
# tightening — keeps the source for "novel first-touch pattern"
# context without amplifying multi-channel fan-outs.


class RecentRoutedEventsReader:
    """Last-N-day dispatch log entries (Chat + Gmail draft fan-outs)."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        lookback_days: int = 7,
        limit: int = 100,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._lookback_days = lookback_days
        self._limit = limit

    def load(self) -> list[RoutedEventRow]:
        sql = _format_sql(
            _ROUTED_EVENTS_SQL,
            project=self._project_id,
            lookback_days=self._lookback_days,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            RoutedEventRow(
                item_id=str(row["item_id"]),
                channel=str(row["channel"]),
                routed_at=row["routed_at"],
            )
            for row in rows
        ]


# ---------------------------------------------------------------- notes

_NOTES_SQL = """
SELECT
  n.note_id,
  n.ingested_at,
  n.filename,
  n.extraction_method,
  n.markdown_content,
  n.source_drive_url
FROM `{project}.agent_outputs.notes` AS n
WHERE n.ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  AND n.hipaa_isolated = FALSE
  AND n.extraction_method != 'failed'
  AND n.note_kind IN ('capture', 'inbox', 'area', 'galaxy')
ORDER BY n.ingested_at DESC
LIMIT @limit
"""
# Narrowed 2026-05-14 — added note_kind filter to keep only
# user-authored content (Drive PKM notes + capture_note submits).
# Excludes:
#   - calendar_event: meetings happen; not accomplishments
#   - email: routing/conversational signal; not wins
#   - decision / win: synthetic rows from ADR 0052 — including them
#     would create a feedback loop where last week's logged wins
#     re-feed into this week's Brag Spotter candidate pool
#   - archive / resource: cold storage + static templates


class RecentNotesReader:
    """Last-N-day non-HIPAA notes with successful extraction."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        lookback_days: int = 7,
        limit: int = 30,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._lookback_days = lookback_days
        self._limit = limit

    def load(self) -> list[NoteRow]:
        sql = _format_sql(
            _NOTES_SQL,
            project=self._project_id,
            lookback_days=self._lookback_days,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            NoteRow(
                note_id=str(row["note_id"]),
                ingested_at=row["ingested_at"],
                filename=str(row["filename"]),
                extraction_method=str(row["extraction_method"]),
                markdown_content=row.get("markdown_content"),
                # Nullable column: str(None) would put the literal "None" in
                # the prompt as a bogus URL. Keep it None so the composer's
                # `if n.source_drive_url` guard drops it.
                source_drive_url=row.get("source_drive_url"),
            )
            for row in rows
        ]


# ---------------------------------------------------------------- decisions

_DECISIONS_SQL = """
SELECT
  d.decision_id,
  d.decided_at,
  d.title,
  d.context,
  d.choice,
  d.status
FROM `{project}.agent_outputs.decisions` AS d
WHERE d.decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  AND (
    d.status = 'confirmed'
    OR d.status LIKE 'reviewed_%'
  )
ORDER BY d.decided_at DESC
LIMIT @limit
"""
# Narrowed 2026-05-14 — previously included 'pending' which is the
# original-spec value for a decision that's been refined but not
# acted on. Confirmed/reviewed_* are the actually-committed ones:
# the user took action. Pending = not-yet-a-win. Matches the new
# MCP world (insert_decision writes status='drafted';
# mark_decision_status transitions to 'confirmed' / 'dismissed').


class RecentDecisionsReader:
    """Last-N-day refined decisions (skips drafts — those are unrefined)."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        lookback_days: int = 7,
        limit: int = 30,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._lookback_days = lookback_days
        self._limit = limit

    def load(self) -> list[DecisionRow]:
        sql = _format_sql(
            _DECISIONS_SQL,
            project=self._project_id,
            lookback_days=self._lookback_days,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            DecisionRow(
                decision_id=str(row["decision_id"]),
                decided_at=row["decided_at"],
                title=str(row["title"]),
                context=row.get("context"),
                choice=str(row["choice"]),
                status=str(row["status"]),
            )
            for row in rows
        ]


# ---------------------------------------------------------------- reflections

_REFLECTIONS_SQL = """
SELECT
  r.reflection_id,
  r.generated_at,
  r.local_date,
  r.body_markdown,
  r.sections_used
FROM `{project}.agent_outputs.evening_reflections` AS r
WHERE r.generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  AND COALESCE(r.mode, 'reflect') = 'reflect'
  AND COALESCE(r.dedup_skipped, FALSE) = FALSE
  AND r.success = TRUE
ORDER BY r.generated_at DESC
LIMIT @limit
"""


class RecentReflectionsReader:
    """Last-N-day successful reflect-mode reflections (skips prompt mode)."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        lookback_days: int = 7,
        limit: int = 14,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._lookback_days = lookback_days
        self._limit = limit

    def load(self) -> list[ReflectionRow]:
        sql = _format_sql(
            _REFLECTIONS_SQL,
            project=self._project_id,
            lookback_days=self._lookback_days,
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            ReflectionRow(
                reflection_id=str(row["reflection_id"]),
                generated_at=row["generated_at"],
                local_date=_coerce_date(row["local_date"]),
                body_markdown=str(row.get("body_markdown") or ""),
                sections_used=tuple(row.get("sections_used") or ()),
            )
            for row in rows
        ]


# ---------------------------------------------------------------- existing wins

_EXISTING_WINS_SQL = """
SELECT
  w.win_id,
  w.title,
  w.source_kind,
  w.source_id
FROM `{project}.agent_outputs.wins` AS w
WHERE w.week_of = DATE('{week_of}')
ORDER BY w.captured_at ASC
LIMIT @limit
"""


class ExistingWinsForWeekReader:
    """Wins already captured for `week_of` (ADR 0043 §5 dedup pre-check)."""

    def __init__(
        self,
        *,
        bq_client: BQQueryClient,
        project_id: str,
        limit: int = 200,
    ) -> None:
        self._bq = bq_client
        self._project_id = project_id
        self._limit = limit

    def load(self, week_of: date) -> list[ExistingWinRow]:
        sql = _format_sql(
            _EXISTING_WINS_SQL,
            project=self._project_id,
            week_of=week_of.isoformat(),
            limit=self._limit,
        )
        rows = self._bq.query_rows(sql)
        return [
            ExistingWinRow(
                win_id=str(row["win_id"]),
                title=str(row["title"]),
                source_kind=str(row["source_kind"]),
                source_id=row.get("source_id"),
            )
            for row in rows
        ]


# ---------------------------------------------------------------- helpers


def _format_sql(template: str, **kwargs: object) -> str:
    """Render a parameterized SQL template with literal scalar values.

    Mirrors `morning_brief.readers._format_sql`. All callers pass
    operator-controlled values (project_id, integer lookback / limit,
    ISO date strings — week_of). No user-facing input reaches this
    function, so the literal-substitution shape is acceptable.
    """
    rendered = template.replace("{project}", str(kwargs["project"]))
    if "lookback_days" in kwargs:
        rendered = rendered.replace("@lookback_days", str(int(kwargs["lookback_days"])))
    if "limit" in kwargs:
        rendered = rendered.replace("@limit", str(int(kwargs["limit"])))
    if "week_of" in kwargs:
        # week_of is ISO format (`YYYY-MM-DD`) — single-quoted in the
        # template, no embedded quote possible.
        rendered = rendered.replace("{week_of}", str(kwargs["week_of"]))
    return rendered


def _coerce_date(raw: object) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    raise TypeError(f"cannot coerce {type(raw).__name__} to date")
