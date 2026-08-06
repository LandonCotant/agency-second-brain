"""Unit tests for the WS-D routing matrix MVP."""

from __future__ import annotations

import pytest
from agency_brain.routing import (
    Channel,
    TriagedItem,
    build_risk_flags_poll_query,
    build_triaged_items_poll_query,
    route_item,
)


def _item(
    severity: str,
    *,
    owner_email: str | None = "owner@example.com",
    human_review_routed: bool = False,
) -> TriagedItem:
    return TriagedItem(
        item_id=f"item-{severity}",
        severity=severity,
        owner_type="delegate" if owner_email else "na",
        owner_email=owner_email,
        human_review_routed=human_review_routed,
    )


def test_critical_routes_to_immediate_owner_and_leadership() -> None:
    decision = route_item(_item("critical"))

    assert decision.owner_view is not None
    assert {i.channel for i in decision.owner_view.intents} == {
        Channel.GEMINI_INBOX,
        Channel.GOOGLE_CHAT_DM,
        Channel.GMAIL_DRAFT,
    }
    assert {i.cadence for i in decision.leadership_view.intents} == {"immediate"}


def test_critical_routes_include_gmail_draft() -> None:
    """ADR 0032: critical signals get a pre-drafted Gmail reply."""
    decision = route_item(_item("critical"))
    channels = {i.channel for i in decision.leadership_view.intents}
    assert Channel.GMAIL_DRAFT in channels


def test_high_routes_to_morning_brief_chat_and_gmail() -> None:
    decision = route_item(_item("high"))

    assert {i.channel for i in decision.leadership_view.intents} == {
        Channel.MORNING_BRIEF,
        Channel.GOOGLE_CHAT_DM,
        Channel.GMAIL_DRAFT,
    }
    assert "same_day_if_before_4pm" in {i.cadence for i in decision.leadership_view.intents}


def test_high_routes_include_gmail_draft() -> None:
    """ADR 0032: high signals get a pre-drafted Gmail reply too."""
    decision = route_item(_item("high"))
    channels = {i.channel for i in decision.leadership_view.intents}
    assert Channel.GMAIL_DRAFT in channels


def test_medium_routes_to_morning_brief() -> None:
    decision = route_item(_item("medium"))

    assert decision.leadership_view.intents == decision.owner_view.intents
    assert [i.channel for i in decision.leadership_view.intents] == [Channel.MORNING_BRIEF]


def test_low_routes_to_collapsed_queue() -> None:
    decision = route_item(_item("low"))

    assert [i.channel for i in decision.leadership_view.intents] == [Channel.REST_OF_QUEUE]
    assert decision.leadership_view.intents[0].cadence == "daily_morning_collapsed"


def test_info_routes_to_airtable_view_only() -> None:
    decision = route_item(_item("info", owner_email=None))

    assert decision.owner_view is None
    assert [i.channel for i in decision.leadership_view.intents] == [Channel.AIRTABLE_VIEW]
    assert decision.leadership_view.intents[0].cadence == "always_available"


def test_human_review_flag_is_preserved() -> None:
    decision = route_item(_item("medium", human_review_routed=True))

    assert decision.human_review_routed is True


def test_unknown_severity_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown routing severity"):
        route_item(_item("urgent"))


def test_polling_query_reads_recent_triaged_items() -> None:
    sql = build_triaged_items_poll_query(
        project_id="agency-brain-demo",
        lookback_minutes=15,
        limit=25,
    )

    assert "`agency-brain-demo.agent_outputs.triaged_items`" in sql
    assert "INTERVAL 15 MINUTE" in sql
    assert "LIMIT 25" in sql
    assert "human_review_routed" in sql


def test_dedup_window_is_decoupled_from_poll_window() -> None:
    """The routed_events dedup subquery must use a WIDE window independent
    of the poll lookback. If they shared a window, widening the poll for a
    backfill would let already-dispatched items fall outside dedup and
    re-fire. Poll WHERE uses the small lookback; dedup uses the 7d floor."""
    sql = build_triaged_items_poll_query(
        project_id="p",
        lookback_minutes=15,
        channels_to_check=("google_chat_dm",),
    )
    # Poll predicate keeps the requested 15-minute window.
    assert "ti.triaged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)" in sql
    # Dedup subquery uses the wide floor (7d = 10080 min), not 15.
    assert "INTERVAL 10080 MINUTE" in sql


def test_dedup_window_never_narrower_than_poll_window() -> None:
    """If the poll lookback exceeds the floor, the dedup window matches it
    (max of the two) so dedup always covers at least the poll window."""
    sql = build_triaged_items_poll_query(
        project_id="p",
        lookback_minutes=20000,  # > 7d floor
        exclude_channel="google_chat_dm",
    )
    # Both the poll predicate and the dedup join use 20000.
    assert sql.count("INTERVAL 20000 MINUTE") == 2
    assert "INTERVAL 10080 MINUTE" not in sql


def test_polling_query_validates_bounds() -> None:
    with pytest.raises(ValueError, match="lookback_minutes"):
        build_triaged_items_poll_query(project_id="p", lookback_minutes=0)
    with pytest.raises(ValueError, match="limit"):
        build_triaged_items_poll_query(project_id="p", limit=0)


# ---------------------------------------------------------------------------
# build_risk_flags_poll_query — ADR 0033 PR-C
# ---------------------------------------------------------------------------


def test_risk_flags_poll_query_targets_correct_tables() -> None:
    sql = build_risk_flags_poll_query(
        project_id="agency-brain-demo",
        lookback_minutes=1440,
        severities=("critical", "high"),
    )

    assert "`agency-brain-demo.agent_outputs.risk_flags`" in sql
    # LEFT JOIN to accounts gives us company_name for the Gmail subject.
    assert "LEFT JOIN `agency-brain-demo.airtable_replica.accounts`" in sql
    assert "a.company_name AS account_name" in sql
    assert "rf.severity IN ('critical', 'high')" in sql
    assert "INTERVAL 1440 MINUTE" in sql
    # Risk-flag-keyed dispatch: routed_events.item_id holds flag_id.
    # No routed_channels subquery without channels_to_check.
    assert "routed_channels" not in sql


def test_risk_flags_poll_query_emits_routed_channels_array_when_requested() -> None:
    sql = build_risk_flags_poll_query(
        project_id="prj",
        lookback_minutes=1440,
        channels_to_check=("google_chat_dm", "gmail_draft"),
    )

    # Channels deduplicated + sorted for snapshot stability.
    assert "re.channel IN ('gmail_draft', 'google_chat_dm')" in sql
    # Dedup keys on flag_id (routed_events.item_id is the source-id col).
    assert "re.item_id = rf.flag_id" in sql
    assert "AS routed_channels" in sql


def test_risk_flags_poll_query_default_lookback_is_24h() -> None:
    """Risk Watcher fires once a day; long lookback covers the post-fire
    morning ramp + Chat severity-window gate (06:00 PT fire → 09:00 PT
    Chat window opens → routing tick must still see the flag)."""
    sql = build_risk_flags_poll_query(project_id="p")
    assert "INTERVAL 1440 MINUTE" in sql


def test_risk_flags_poll_query_validates_bounds() -> None:
    with pytest.raises(ValueError, match="lookback_minutes"):
        build_risk_flags_poll_query(project_id="p", lookback_minutes=0)
    with pytest.raises(ValueError, match="limit"):
        build_risk_flags_poll_query(project_id="p", limit=0)
    with pytest.raises(ValueError, match="severities"):
        build_risk_flags_poll_query(project_id="p", severities=())
    with pytest.raises(ValueError, match="unknown severities"):
        build_risk_flags_poll_query(project_id="p", severities=("urgent",))
    with pytest.raises(ValueError, match="channels_to_check"):
        build_risk_flags_poll_query(project_id="p", channels_to_check=())
    with pytest.raises(ValueError, match="unknown channels"):
        build_risk_flags_poll_query(project_id="p", channels_to_check=("slack",))


def test_risk_flags_poll_query_excludes_resolved_flags() -> None:
    """ADR 0035 §5: resolved flags don't dispatch.

    Manual cleanup of false positives (or future agent-side
    resolution) sets ``risk_flags.resolved_at`` so the routing fan-out
    skips them without DDL or schema changes.
    """
    sql = build_risk_flags_poll_query(project_id="p")
    assert "rf.resolved_at IS NULL" in sql
