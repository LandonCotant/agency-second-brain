"""Pure routing matrix for WS-D.

This module converts a classified triage item into owner-view and
leadership-view route intents. Delivery adapters are deliberately deferred;
callers can persist or act on these intents in later WS-D PRs.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Channel(enum.StrEnum):
    GEMINI_INBOX = "gemini_inbox"
    GOOGLE_CHAT_DM = "google_chat_dm"
    GMAIL_DRAFT = "gmail_draft"
    MORNING_BRIEF = "morning_brief"
    REST_OF_QUEUE = "rest_of_queue"
    AIRTABLE_VIEW = "airtable_view"


@dataclass(frozen=True)
class TriagedItem:
    item_id: str
    severity: str
    owner_type: str
    owner_email: str | None
    human_review_routed: bool


@dataclass(frozen=True)
class RouteIntent:
    channel: Channel
    cadence: str
    reason: str


@dataclass(frozen=True)
class RoutingView:
    audience: str
    recipient_email: str | None
    intents: tuple[RouteIntent, ...]


@dataclass(frozen=True)
class RoutingDecision:
    item_id: str
    severity: str
    human_review_routed: bool
    owner_view: RoutingView | None
    leadership_view: RoutingView


_MATRIX: dict[str, tuple[RouteIntent, ...]] = {
    "critical": (
        RouteIntent(
            channel=Channel.GEMINI_INBOX,
            cadence="immediate",
            reason="Critical items need immediate push visibility.",
        ),
        RouteIntent(
            channel=Channel.GOOGLE_CHAT_DM,
            cadence="immediate",
            reason="Critical items need immediate Chat visibility.",
        ),
        RouteIntent(
            channel=Channel.GMAIL_DRAFT,
            cadence="immediate",
            reason="Critical items get a pre-drafted reply for 1-click response.",
        ),
    ),
    "high": (
        RouteIntent(
            channel=Channel.MORNING_BRIEF,
            cadence="daily_morning",
            reason="High items appear in the next morning brief.",
        ),
        RouteIntent(
            channel=Channel.GOOGLE_CHAT_DM,
            cadence="same_day_if_before_4pm",
            reason="High items may need same-day attention.",
        ),
        RouteIntent(
            channel=Channel.GMAIL_DRAFT,
            cadence="same_day_if_actionable",
            reason="High items get a pre-drafted reply when actionable.",
        ),
    ),
    "medium": (
        RouteIntent(
            channel=Channel.MORNING_BRIEF,
            cadence="daily_morning",
            reason="Medium items are reviewed in the morning brief.",
        ),
    ),
    "low": (
        RouteIntent(
            channel=Channel.REST_OF_QUEUE,
            cadence="daily_morning_collapsed",
            reason="Low items belong in the collapsed queue.",
        ),
    ),
    "info": (
        RouteIntent(
            channel=Channel.AIRTABLE_VIEW,
            cadence="always_available",
            reason="Info items are retained for review without push.",
        ),
    ),
}


def route_item(
    item: TriagedItem,
    *,
    leadership_email: str = "owner@example.com",
) -> RoutingDecision:
    """Return owner and leadership routing views for one triaged item."""
    severity = item.severity.lower()
    try:
        intents = _MATRIX[severity]
    except KeyError as exc:
        raise ValueError(f"unknown routing severity: {item.severity!r}") from exc

    owner_view = None
    if item.owner_email:
        owner_view = RoutingView(
            audience="owner",
            recipient_email=item.owner_email,
            intents=intents,
        )

    return RoutingDecision(
        item_id=item.item_id,
        severity=severity,
        human_review_routed=item.human_review_routed,
        owner_view=owner_view,
        leadership_view=RoutingView(
            audience="leadership",
            recipient_email=leadership_email,
            intents=intents,
        ),
    )
