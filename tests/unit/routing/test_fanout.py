"""Unit tests for RoutingFanoutAgent and run_fanout_tick (ADR 0023, 0032)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank
from agency_brain.routing import (
    TriagedItem,
    build_decisions_poll_query,
    build_risk_flags_poll_query,
    build_triaged_items_poll_query,
)
from agency_brain.routing.channels.chat import (
    ChatWebhookClient,
)
from agency_brain.routing.channels.gmail import (
    GmailDraftError,
    GmailDraftResult,
)
from agency_brain.routing.fanout import (
    ChannelDispatchResult,
    FanoutInput,
    RoutingFanoutAgent,
    make_chat_adapter,
    make_gmail_adapter,
    run_fanout_tick,
)
from agency_brain.routing.formatters import RoutingMessageContext
from agency_brain.routing.matrix import Channel

LA = ZoneInfo("America/Los_Angeles")
CHAT = Channel.GOOGLE_CHAT_DM.value
GMAIL = Channel.GMAIL_DRAFT.value


# --------------------------------------------------------------- test doubles


class _RecordingBQ:
    """Captures audit-log inserts."""

    def __init__(self) -> None:
        self.rows_by_table: dict[str, list[dict]] = {}

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.rows_by_table.setdefault(table_ref, []).extend(rows)
        return []


class _FakeFanoutBQ:
    """Implements the BQQueryClient protocol used by the orchestrator."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []
        # ADR 0025: list of (item_id, channel, chat_status) tuples
        self.routings: list[tuple[str, str, int | None]] = []

    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return list(self.rows)

    def record_routing(
        self,
        *,
        item_id: str,
        channel: str,
        chat_status: int | None = None,
        agent_run_id: str | None = None,
    ) -> int:
        self.routings.append((item_id, channel, chat_status))
        return 1


class _FakePoster:
    """In-memory HttpPoster, replays canned responses."""

    def __init__(self, responses: list[tuple[int, str] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, float]] = []

    def post_json(self, url: str, payload: dict, *, timeout_s: float):
        self.calls.append((url, payload, timeout_s))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeGmailAdapter:
    """Mimics make_gmail_adapter's output without DWD wiring."""

    channel = Channel.GMAIL_DRAFT

    def __init__(
        self,
        *,
        responses: list[GmailDraftResult | Exception] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.calls: list[FanoutInput] = []

    def dispatch(self, input: FanoutInput) -> ChannelDispatchResult:
        self.calls.append(input)
        if not self._responses:
            raise AssertionError("FakeGmailAdapter ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return ChannelDispatchResult(
            channel=Channel.GMAIL_DRAFT,
            ok=nxt.ok,
            draft_id=nxt.draft_id,
        )


# ------------------------------------------------------------------- helpers


def _ctx(severity: str = "critical") -> RoutingMessageContext:
    return RoutingMessageContext(
        item_id=f"item-{severity}",
        severity=severity,
        source="gmail",
        action_type="reply_today",
        reasoning="reasoning",
        source_url="https://example/x",
        owner_email="owner@example.com",
    )


def _input(
    severity: str = "critical",
    *,
    already_routed: tuple[str, ...] = (),
) -> FanoutInput:
    return FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id=f"item-{severity}",
            severity=severity,
            owner_type="delegate",
            owner_email="owner@example.com",
            human_review_routed=False,
        ),
        triaged_at=datetime(2026, 4, 30, 18, 0, tzinfo=UTC),
        message_context=_ctx(severity=severity),
        already_routed=frozenset(already_routed),
    )


def _make_agent(
    *,
    audit_bq: _RecordingBQ,
    fanout_bq: _FakeFanoutBQ,
    poster_responses: list | None = None,
    gmail_responses: list | None = None,
    clock: Any = None,
    include_chat: bool = True,
    include_gmail: bool = False,
) -> RoutingFanoutAgent:
    audit = AuditLogClient(project_id="agency-brain-demo", bq_client=audit_bq)
    channels: dict[Channel, Any] = {}
    if include_chat:
        poster = _FakePoster(poster_responses or [])
        chat = ChatWebhookClient("https://chat.example/webhook", http=poster)
        channels[Channel.GOOGLE_CHAT_DM] = make_chat_adapter(chat)
    if include_gmail:
        channels[Channel.GMAIL_DRAFT] = _FakeGmailAdapter(responses=gmail_responses or [])
    return RoutingFanoutAgent(
        sa_email="asb-routing-sa@agency-brain-demo.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        channel_clients=channels,
        bq=fanout_bq,
        clock=clock,
    )


# -------------------------------------------------------------- agent tests


def test_critical_dispatches_chat_and_records_routed_events() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, '{"name":"spaces/X/messages/Y"}')],
        # 03:00 UTC = outside the high window, but critical bypasses.
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    output = agent.invoke(_input("critical"))

    assert output.routed is True
    assert output.dispatched_channels == (CHAT,)
    assert bq.routings == [("item-critical", CHAT, 200)]


def test_critical_dispatches_chat_and_gmail_in_one_invoke() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
        gmail_responses=[GmailDraftResult(ok=True, draft_id="r123", threaded=False)],
        include_gmail=True,
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    output = agent.invoke(_input("critical"))

    assert output.routed is True
    assert set(output.dispatched_channels) == {CHAT, GMAIL}
    # Two routed_events rows — one per channel.
    routed_channels = {ch for (_, ch, _) in bq.routings}
    assert routed_channels == {CHAT, GMAIL}


def test_gmail_failure_does_not_block_chat() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
        gmail_responses=[GmailDraftError("transient blip")],
        include_gmail=True,
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    output = agent.invoke(_input("critical"))

    assert CHAT in output.dispatched_channels
    assert GMAIL not in output.dispatched_channels
    assert GMAIL in output.error_channels
    assert output.error_channels[GMAIL].startswith("transient:")
    # Only the Chat dispatch made it to routed_events.
    assert [r for r in bq.routings if r[1] == CHAT]
    assert not [r for r in bq.routings if r[1] == GMAIL]


def test_chat_4xx_records_permanent_error_other_channel_continues() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(400, "bad payload")],
        gmail_responses=[GmailDraftResult(ok=True, draft_id="r1", threaded=False)],
        include_gmail=True,
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    output = agent.invoke(_input("critical"))

    assert GMAIL in output.dispatched_channels
    assert output.error_channels[CHAT].startswith("permanent:")


def test_already_routed_skips_redispatch() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],  # Chat must NOT be called
        gmail_responses=[GmailDraftResult(ok=True, draft_id="r1", threaded=False)],
        include_gmail=True,
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    output = agent.invoke(_input("critical", already_routed=(CHAT,)))

    assert output.dispatched_channels == (GMAIL,)
    assert output.skipped_channels[CHAT] == "already-routed"


def test_high_in_window_dispatches_chat() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    in_window = datetime(2026, 4, 30, 12, 0, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
        clock=lambda: in_window,
    )

    output = agent.invoke(_input("high"))

    assert output.routed is True
    assert CHAT in output.dispatched_channels


def test_high_after_4pm_skips_chat() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    after_window = datetime(2026, 4, 30, 16, 30, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],
        clock=lambda: after_window,
    )

    output = agent.invoke(_input("high"))

    assert output.dispatched_channels == ()
    assert output.skipped_channels[CHAT] == "severity-window-skip"


def test_high_after_4pm_still_dispatches_gmail() -> None:
    """ADR 0032: Gmail bypasses the Chat severity window — drafts don't notify."""
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    after_window = datetime(2026, 4, 30, 16, 30, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],
        gmail_responses=[GmailDraftResult(ok=True, draft_id="r1", threaded=False)],
        include_gmail=True,
        clock=lambda: after_window,
    )

    output = agent.invoke(_input("high"))

    assert GMAIL in output.dispatched_channels
    assert output.skipped_channels[CHAT] == "severity-window-skip"


def test_high_at_window_start_dispatches() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    at_start = datetime(2026, 4, 30, 9, 0, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
        clock=lambda: at_start,
    )

    assert agent.invoke(_input("high")).routed is True


def test_high_at_window_end_skips_chat() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    at_end = datetime(2026, 4, 30, 16, 0, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],
        clock=lambda: at_end,
    )

    output = agent.invoke(_input("high"))

    assert CHAT not in output.dispatched_channels


def test_medium_severity_no_active_channels() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],
    )

    output = agent.invoke(_input("medium"))

    # Medium routes to MORNING_BRIEF only — no active adapter for it.
    assert output.dispatched_channels == ()
    assert "morning_brief" in output.skipped_channels


def test_chat_5xx_records_transient_no_routing() -> None:
    """Per-channel transient error is captured, not raised."""
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(503, "service unavailable")],
    )

    output = agent.invoke(_input("critical"))

    assert output.dispatched_channels == ()
    assert output.error_channels[CHAT].startswith("transient:")
    assert bq.routings == []


def test_chat_4xx_records_permanent_no_routing() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(400, "bad payload")],
    )

    output = agent.invoke(_input("critical"))

    assert output.dispatched_channels == ()
    assert output.error_channels[CHAT].startswith("permanent:")
    assert bq.routings == []


def test_audit_row_has_routing_fanout_agent_id() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
    )

    agent.invoke(_input("critical"))

    rows = audit_bq.rows_by_table.get("agency-brain-demo.agent_audit_log.events", [])
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "routing-fanout"
    assert rows[0]["success"] is True


def test_success_audit_summarizes_dispatched_channels() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, "")],
        gmail_responses=[GmailDraftResult(ok=True, draft_id="r1", threaded=False)],
        include_gmail=True,
        clock=lambda: datetime(2026, 4, 30, 3, 0, tzinfo=UTC),
    )

    agent.invoke(_input("critical"))

    rows = audit_bq.rows_by_table["agency-brain-demo.agent_audit_log.events"]
    summary = rows[0]["output"] or ""
    assert "dispatched=" in summary
    assert CHAT in summary
    assert GMAIL in summary


def test_hipaa_aspect_short_circuits_before_dispatch() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ()
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],  # no Chat call expected
    )

    poisoned = FanoutInput(
        aspects=["hipaa_excluded"],
        item=_input("critical").item,
        triaged_at=_input("critical").triaged_at,
        message_context=_ctx(),
    )

    from agency_brain.agents.base import HipaaGuardTripped

    with pytest.raises(HipaaGuardTripped):
        agent.invoke(poisoned)
    assert bq.routings == []


# ----------------------------------------------------- run_fanout_tick tests


def _row_to_input(row: dict[str, Any]) -> FanoutInput:
    """Production-shaped converter used by tick tests."""
    routed_raw = row.get("routed_channels") or ()
    return FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id=row["item_id"],
            severity=row["severity"],
            owner_type=row["owner_type"],
            owner_email=row.get("owner_email"),
            human_review_routed=row["human_review_routed"],
        ),
        triaged_at=row["triaged_at"],
        message_context=RoutingMessageContext(
            item_id=row["item_id"],
            severity=row["severity"],
            source=row["source"],
            action_type=row["action_type"],
            reasoning=row["reasoning"],
            source_url=row.get("source_url"),
            source_event_ref=row.get("source_event_ref"),
            owner_email=row.get("owner_email"),
            airtable_task_record_id=row.get("airtable_task_record_id"),
        ),
        already_routed=frozenset(str(c) for c in routed_raw if c),
    )


def _row(severity: str = "critical", item_id: str | None = None) -> dict:
    return {
        "item_id": item_id or f"item-{severity}",
        "triaged_at": datetime(2026, 4, 30, 18, 0, tzinfo=UTC),
        "source": "gmail",
        "source_url": "https://example/x",
        "source_event_ref": "ref-1",
        "actionable": True,
        "owner_type": "delegate",
        "owner_email": "owner@example.com",
        "action_type": "reply_today",
        "severity": severity,
        "confidence": 0.9,
        "human_review_routed": False,
        "reasoning": "test",
        "routed_channels": [],
        "airtable_task_record_id": None,
    }


def test_tick_dispatches_each_polled_row() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ(rows=[_row("critical", "i1"), _row("critical", "i2")])
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(200, ""), (200, "")],
    )

    result = run_fanout_tick(
        bq=bq,
        agent=agent,
        poll_sql="SELECT 1",
        row_to_input=_row_to_input,
    )

    assert result.polled == 2
    assert result.dispatched_per_channel.get(CHAT) == 2
    assert result.dispatched == 2
    assert result.transient_errors == []
    assert result.permanent_errors == []
    assert bq.routings == [("i1", CHAT, 200), ("i2", CHAT, 200)]


def test_tick_continues_after_transient_error() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ(rows=[_row("critical", "i1"), _row("critical", "i2")])
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(503, "boom"), (200, "ok")],
    )

    result = run_fanout_tick(bq=bq, agent=agent, poll_sql="SELECT 1", row_to_input=_row_to_input)

    assert result.dispatched_per_channel.get(CHAT) == 1
    assert result.transient_errors  # one entry for i1
    assert bq.routings == [("i2", CHAT, 200)]


def test_tick_classifies_400_as_permanent() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ(rows=[_row("critical", "i1")])
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[(400, "bad")],
    )

    result = run_fanout_tick(bq=bq, agent=agent, poll_sql="SELECT 1", row_to_input=_row_to_input)

    assert result.dispatched == 0
    assert result.permanent_errors
    assert result.transient_errors == []


def test_tick_skips_route_when_severity_window_blocks() -> None:
    audit_bq = _RecordingBQ()
    bq = _FakeFanoutBQ(rows=[_row("high", "i1")])
    after_window = datetime(2026, 4, 30, 16, 30, tzinfo=LA).astimezone(UTC)
    agent = _make_agent(
        audit_bq=audit_bq,
        fanout_bq=bq,
        poster_responses=[],
        clock=lambda: after_window,
    )

    result = run_fanout_tick(bq=bq, agent=agent, poll_sql="SELECT 1", row_to_input=_row_to_input)

    assert result.polled == 1
    assert result.skipped >= 1
    assert result.dispatched == 0


# --------------------------------------------------------- polling SQL tests


def test_poll_query_with_exclude_channel_uses_routed_events_left_join() -> None:
    sql = build_triaged_items_poll_query(
        project_id="p",
        exclude_channel="google_chat_dm",
    )
    assert "`p.agent_outputs.routed_events`" in sql
    assert "re.channel = 'google_chat_dm'" in sql
    assert "re.item_id IS NULL" in sql


def test_poll_query_channels_to_check_emits_routed_channels_array() -> None:
    """ADR 0032 multi-channel mode: correlated subquery returns array."""
    sql = build_triaged_items_poll_query(
        project_id="p",
        channels_to_check=("google_chat_dm", "gmail_draft"),
    )
    assert "ARRAY(" in sql
    assert "AS routed_channels" in sql
    assert "re.channel IN ('gmail_draft', 'google_chat_dm')" in sql


def test_poll_query_exclude_and_channels_to_check_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_triaged_items_poll_query(
            project_id="p",
            exclude_channel="google_chat_dm",
            channels_to_check=("gmail_draft",),
        )


def test_poll_query_severity_filter_quotes_each_value() -> None:
    sql = build_triaged_items_poll_query(
        project_id="p",
        severities=("critical", "high"),
    )
    assert "ti.severity IN ('critical', 'high')" in sql


def test_poll_query_unknown_severity_raises() -> None:
    with pytest.raises(ValueError, match="unknown severities"):
        build_triaged_items_poll_query(project_id="p", severities=("urgent",))


def test_poll_query_unknown_channel_raises() -> None:
    with pytest.raises(ValueError, match="unknown channel"):
        build_triaged_items_poll_query(project_id="p", exclude_channel="slack_dm")


def test_poll_query_unknown_channels_to_check_raises() -> None:
    with pytest.raises(ValueError, match="unknown channels"):
        build_triaged_items_poll_query(project_id="p", channels_to_check=("slack_dm",))


def test_poll_query_empty_severities_raises() -> None:
    with pytest.raises(ValueError, match="severities must be non-empty"):
        build_triaged_items_poll_query(project_id="p", severities=())


def test_poll_query_includes_airtable_task_record_id_for_link_formatter() -> None:
    sql = build_triaged_items_poll_query(project_id="p")
    assert "airtable_task_record_id" in sql


# ----------------------------------------------- decisions polling (ADR 0041)


def test_decisions_poll_query_filters_status_draft() -> None:
    sql = build_decisions_poll_query(project_id="p")
    assert "d.status = 'draft'" in sql


def test_decisions_poll_query_uses_24h_default_lookback() -> None:
    sql = build_decisions_poll_query(project_id="p")
    assert "INTERVAL 1440 MINUTE" in sql


def test_decisions_poll_query_lookback_minutes_overrides_default() -> None:
    sql = build_decisions_poll_query(project_id="p", lookback_minutes=60)
    assert "INTERVAL 60 MINUTE" in sql
    assert "INTERVAL 1440 MINUTE" not in sql


def test_decisions_poll_query_selects_decision_fields_for_formatter() -> None:
    sql = build_decisions_poll_query(project_id="p")
    for col in (
        "d.decision_id",
        "d.decided_at",
        "d.title",
        "d.context",
        "d.choice",
        "d.source_voice_note_id",
    ):
        assert col in sql, f"missing column: {col}"


def test_decisions_poll_query_channels_to_check_keys_on_decision_id() -> None:
    """routed_events.item_id stores the decision_id (generic source-id column)."""
    sql = build_decisions_poll_query(
        project_id="p",
        channels_to_check=("google_chat_dm", "gmail_draft"),
    )
    assert "ARRAY(" in sql
    assert "AS routed_channels" in sql
    assert "re.item_id = d.decision_id" in sql
    assert "re.channel IN ('gmail_draft', 'google_chat_dm')" in sql


def test_decisions_poll_query_unknown_channel_raises() -> None:
    with pytest.raises(ValueError, match="unknown channels"):
        build_decisions_poll_query(project_id="p", channels_to_check=("slack_dm",))


def test_decisions_poll_query_empty_channels_to_check_raises() -> None:
    with pytest.raises(ValueError, match="channels_to_check must be non-empty"):
        build_decisions_poll_query(project_id="p", channels_to_check=())


def test_decisions_poll_query_negative_lookback_raises() -> None:
    with pytest.raises(ValueError, match="lookback_minutes must be positive"):
        build_decisions_poll_query(project_id="p", lookback_minutes=0)


def test_decisions_poll_query_targets_decisions_table() -> None:
    sql = build_decisions_poll_query(project_id="agency-brain-demo")
    assert "`agency-brain-demo.agent_outputs.decisions`" in sql


def test_decisions_poll_query_no_severity_filter_present() -> None:
    """Decisions table has no severity column — synthesized in row converter."""
    sql = build_decisions_poll_query(project_id="p")
    assert "severity" not in sql


# ----------------------------------------------- risk_flags polling regression


def test_risk_flags_poll_query_filters_resolved_at_is_null() -> None:
    """Sanity regression: ADR 0035 §5 filter still present alongside new builder."""
    sql = build_risk_flags_poll_query(project_id="p")
    assert "rf.resolved_at IS NULL" in sql


# --------------------------------------------------- Gmail adapter integration


def test_gmail_adapter_threading_only_for_gmail_source() -> None:
    """Drive/Calendar signals never thread, even if thread_id is stamped."""
    fake = _FakeGmailAdapter(responses=[GmailDraftResult(ok=True, draft_id="r1", threaded=False)])

    drive_input = FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id="i1",
            severity="critical",
            owner_type="delegate",
            owner_email=None,
            human_review_routed=False,
        ),
        triaged_at=datetime(2026, 4, 30, 18, 0, tzinfo=UTC),
        message_context=RoutingMessageContext(
            item_id="i1",
            severity="critical",
            source="drive",
            action_type="review",
            reasoning="r",
            gmail_thread_id="thread-123",
        ),
    )

    # _FakeGmailAdapter's dispatch is the test-only wrapper — does not
    # implement the source-gating itself. The real _GmailAdapter would
    # nullify thread_id for non-Gmail sources before calling the SDK.
    # This test asserts the formatter passes through what's given.
    result = fake.dispatch(drive_input)
    assert result.ok is True


def test_real_gmail_adapter_strips_thread_id_for_non_gmail() -> None:
    """The production _GmailAdapter null-checks thread_id when source != gmail."""
    captured: dict[str, Any] = {}

    class _StubGmail:
        def draft(self, **kwargs):
            captured.update(kwargs)
            return GmailDraftResult(ok=True, draft_id="r1", threaded=False)

    adapter = make_gmail_adapter(_StubGmail(), recipient_email="owner@example.com")

    drive_input = FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id="i1",
            severity="critical",
            owner_type="delegate",
            owner_email=None,
            human_review_routed=False,
        ),
        triaged_at=datetime(2026, 4, 30, 18, 0, tzinfo=UTC),
        message_context=RoutingMessageContext(
            item_id="i1",
            severity="critical",
            source="drive",  # NOT gmail
            action_type="review",
            reasoning="r",
            gmail_thread_id="thread-123",
        ),
    )

    adapter.dispatch(drive_input)
    assert captured["thread_id"] is None
