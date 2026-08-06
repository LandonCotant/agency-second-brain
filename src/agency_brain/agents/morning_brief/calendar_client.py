"""Google Calendar reader for the Morning Brief.

Uses Workspace Domain-Wide Delegation impersonation (ADR 0027 + 0029):
the Cloud Run Job runs as `asb-agent-triage-sa`, then mints a downscoped
credential `with_subject(recipient_email)` for the
`https://www.googleapis.com/auth/calendar.readonly` scope. Read-only —
the agent cannot create / modify / delete events; PRD §4.7 boundary
preserved.

Pattern mirrors the writer/reader Protocol shape elsewhere: the client
has one public method, takes a credentials factory at construction so
unit tests can stub it without touching real Workspace.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import CalendarEvent

log = logging.getLogger("agency_brain.agents.morning_brief.calendar_client")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class CalendarServiceFactory(Protocol):
    """Builds a googleapiclient Calendar v3 service for a given subject.

    Production impl wraps `google.auth.impersonated_credentials.Credentials
    .with_subject(subject)` + `googleapiclient.discovery.build('calendar',
    'v3', credentials=...)`. Test impls return a stub object with a
    `.events().list(...).execute()` chain.
    """

    def build(self, subject: str) -> Any: ...


class CalendarClient:
    """Reads `events_for_today` from the recipient's primary calendar."""

    def __init__(self, *, service_factory: CalendarServiceFactory) -> None:
        self._factory = service_factory

    def events_for_today(
        self,
        recipient_email: str,
        local_date: date,
        tz_name: str = "America/Los_Angeles",
    ) -> list[CalendarEvent]:
        """Return events on `local_date` from the recipient's primary calendar.

        Time bounds are 00:00–24:00 in `tz_name`. Recurring events are
        expanded (`singleEvents=True`) so the brief shows the actual
        occurrences happening today, not the recurrence rule.
        """
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            log.warning("calendar_client: unknown tz %r, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")

        start = datetime.combine(local_date, time.min, tzinfo=tz)
        end = datetime.combine(local_date, time.max, tzinfo=tz)

        service = self._factory.build(recipient_email)
        try:
            response = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                )
                .execute()
            )
        except Exception:
            log.exception(
                "calendar_client: events.list failed for %s on %s",
                recipient_email,
                local_date.isoformat(),
            )
            return []

        items = response.get("items", []) if isinstance(response, dict) else []
        out: list[CalendarEvent] = []
        for item in items:
            start_dt = _parse_event_time(item.get("start"), tz)
            end_dt = _parse_event_time(item.get("end"), tz)
            if start_dt is None or end_dt is None:
                continue
            attendees = tuple(
                a.get("email", "")
                for a in item.get("attendees", [])
                if isinstance(a, dict) and a.get("email")
            )
            out.append(
                CalendarEvent(
                    summary=str(item.get("summary", "(no title)")),
                    start=start_dt,
                    end=end_dt,
                    attendees=attendees,
                )
            )
        return out


def _parse_event_time(raw: Any, fallback_tz: ZoneInfo) -> datetime | None:
    """Calendar v3 event times come as either {dateTime, timeZone} or {date}.

    `dateTime` is RFC3339 with a TZ offset. `date` is an all-day event —
    we coerce it to midnight in the recipient's TZ so the brief renders
    consistently.
    """
    if not isinstance(raw, dict):
        return None
    if "dateTime" in raw:
        try:
            return datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
        except ValueError:
            return None
    if "date" in raw:
        try:
            d = date.fromisoformat(raw["date"])
        except ValueError:
            return None
        return datetime.combine(d, time.min, tzinfo=fallback_tz)
    return None
