"""Calendar reader for the Risk Watcher's Owner Disengagement signal.

Uses Workspace Domain-Wide Delegation impersonation per ADR 0034 §4:
the Cloud Run Job runs as ``asb-risk-watcher-sa``, mints a short-lived
token via ``serviceAccountTokenCreator`` on ``asb-agent-triage-sa``
(the only DWD-grantable SA per ADR 0027 §2), and that impersonated
identity carries the ``calendar.readonly`` scope with subject
``owner@example.com``. Read-only — the agent cannot
create / modify / delete events; PRD §4.7 boundary preserved.

ADR 0035 replaced the v1 ``last_meeting_with_domain`` (attendee
domain-suffix match) with ``most_recent_engagement_event`` (exact-
email-set match). The v1 design false-fired on free-mail contact
domains; the v2 design matches the contact's specific email so the
free-mail problem disappears at the source.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger("agency_brain.agents.risk_watcher.calendar_client")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class CalendarServiceFactory(Protocol):
    """Builds a googleapiclient Calendar v3 service for a given subject.

    Production impl is ``common.dwd.DWDServiceFactory`` configured for
    ``calendar.readonly`` against ``asb-agent-triage-sa``. Test impls
    return a stub object with a
    ``.events().list(...).execute()`` chain.
    """

    def build(self, subject: str) -> Any: ...


class CalendarClient:
    """Reads the owner's calendar for engagement-event recency.

    Production callers pass a ``DWDServiceFactory`` already configured
    for the ``calendar.readonly`` scope. Tests inject a stub factory.
    """

    def __init__(self, *, service_factory: CalendarServiceFactory) -> None:
        self._factory = service_factory

    def most_recent_engagement_event(
        self,
        *,
        owner_email: str,
        attendee_emails: tuple[str, ...],
        since: datetime,
        until: datetime,
    ) -> datetime | None:
        """Most recent event start where any attendee email matches.

        ``attendee_emails`` is the CRM contact email list for one
        account (typically 1-3 emails for a 2-person agency's
        clients). Match is case-insensitive exact-set membership on
        the post-``@`` portion is NOT used — we match the full email
        only. This sidesteps the v1 free-mail problem where any
        @gmail.com attendee counted as "the client."

        Returns ``None`` if no matching event in the window OR if the
        Calendar API call fails (the loader logs separately so a
        quiet account is distinguishable from an outage).
        """
        if not attendee_emails:
            return None

        normalized_emails = frozenset(e.lower() for e in attendee_emails if e)
        if not normalized_emails:
            return None

        service = self._factory.build(owner_email)
        try:
            response = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=since.isoformat(),
                    timeMax=until.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                )
                .execute()
            )
        except Exception:
            log.exception(
                "calendar_client: events.list failed for %s window=%s..%s",
                owner_email,
                since.isoformat(),
                until.isoformat(),
            )
            return None

        items = response.get("items", []) if isinstance(response, dict) else []
        latest: datetime | None = None
        for item in items:
            attendees = item.get("attendees", []) or []
            if not _any_attendee_email_matches(attendees, normalized_emails):
                continue
            start_dt = _parse_event_time(item.get("start"))
            if start_dt is None:
                continue
            if latest is None or start_dt > latest:
                latest = start_dt
        return latest


def _any_attendee_email_matches(attendees: list[Any], normalized_emails: frozenset[str]) -> bool:
    for a in attendees:
        if not isinstance(a, dict):
            continue
        email = a.get("email")
        if not isinstance(email, str):
            continue
        if email.lower() in normalized_emails:
            return True
    return False


def _parse_event_time(raw: Any) -> datetime | None:
    """Calendar v3 event times come as either {dateTime, timeZone} or {date}.

    Risk Watcher only uses ``dateTime`` — all-day events with just a
    ``date`` aren't load-bearing for "did the owner show up to a
    meeting" semantics.
    """
    if not isinstance(raw, dict):
        return None
    raw_dt = raw.get("dateTime")
    if not isinstance(raw_dt, str):
        return None
    try:
        return datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    except ValueError:
        return None
