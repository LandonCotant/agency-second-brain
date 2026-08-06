"""Google Calendar v3 client for the Calendar ingester.

Uses ``common.dwd.DWDServiceFactory`` to mint a downscoped credential
with ``calendar.readonly`` impersonating ``asb-agent-triage-sa``. ADR
0027 §3 invariant preserved.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from .models import CalendarEvent

log = logging.getLogger("agency_brain.agents.calendar_ingester.calendar_client")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class CalendarServiceFactory(Protocol):
    def build(self, subject: str) -> Any: ...


class CalendarClient:
    """Lists events from a single calendar between two timestamps."""

    def __init__(
        self,
        *,
        factory: CalendarServiceFactory,
        subject: str,
    ) -> None:
        self._factory = factory
        self._subject = subject

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        max_per_page: int = 250,
    ) -> list[CalendarEvent]:
        service = self._factory.build(self._subject)
        events: list[CalendarEvent] = []
        page_token: str | None = None
        while True:
            response = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=_iso_z(time_min),
                    timeMax=_iso_z(time_max),
                    singleEvents=True,
                    showDeleted=False,
                    orderBy="startTime",
                    maxResults=max_per_page,
                    pageToken=page_token,
                )
                .execute()
            )
            for raw in (response or {}).get("items", []):
                ev = _normalize_event(raw, calendar_id=calendar_id)
                if ev is not None:
                    events.append(ev)
            page_token = (response or {}).get("nextPageToken")
            if not page_token:
                break
        return events


def _normalize_event(raw: dict[str, Any], *, calendar_id: str) -> CalendarEvent | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("status") == "cancelled":
        return None
    event_id = str(raw.get("id") or "")
    if not event_id:
        return None
    summary = str(raw.get("summary") or "")
    description = str(raw.get("description") or "")
    organizer = str((raw.get("organizer") or {}).get("email") or "").lower()
    location = str(raw.get("location") or "")
    status = str(raw.get("status") or "")
    html_link = str(raw.get("htmlLink") or "")
    attendees: list[str] = []
    for a in raw.get("attendees") or []:
        email = ((a or {}).get("email") or "").lower().strip()
        if email:
            attendees.append(email)
    start = _decode_when(raw.get("start"))
    end = _decode_when(raw.get("end"))
    updated_at = _decode_iso(raw.get("updated"))
    return CalendarEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        summary=summary,
        description=description,
        start=start,
        end=end,
        attendees=tuple(attendees),
        organizer=organizer,
        location=location,
        status=status,
        html_link=html_link,
        updated_at=updated_at,
    )


def _decode_when(raw: Any) -> datetime | None:
    if not isinstance(raw, dict):
        return None
    # Calendar v3 event.start/end is either {"dateTime": "..."} or
    # {"date": "YYYY-MM-DD"} (all-day event).
    iso = raw.get("dateTime") or raw.get("date")
    if not iso:
        return None
    return _decode_iso(iso)


def _decode_iso(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # All-day events come back as "YYYY-MM-DD" without time/tz.
        try:
            return datetime.fromisoformat(f"{s}T00:00:00+00:00")
        except ValueError:
            return None


def _iso_z(dt: datetime) -> str:
    """Calendar v3 expects RFC3339 with explicit timezone."""
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.isoformat()
