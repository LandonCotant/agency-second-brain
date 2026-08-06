"""Routing fan-out orchestrator (ADR 0023, ADR 0025, ADR 0032).

Per-tick flow on Cloud Run Job ``asb-routing-fanout`` (every 5 min):

1. Poll ``agent_outputs.triaged_items`` for unrouted critical/high rows
   via :func:`routing.polling.build_triaged_items_poll_query`. The
   query also returns a ``routed_channels`` array for per-channel
   dedup (ADR 0032 multi-channel mode).
2. For each row, run :class:`RoutingFanoutAgent.invoke`. The agent
   applies :func:`routing.matrix.route_item`, gates Chat by the
   severity/time-window policy, and dispatches to every channel in
   the matrix that has a registered :class:`ChannelClient` adapter
   and has not yet been recorded in ``routed_events``.
3. Each invocation emits one ``agent_audit_log.events`` row for free
   via :class:`BaseAgent`. One ``routed_events`` row is INSERTed per
   successful per-channel dispatch (ADR 0025).

The orchestrator is intentionally thin: BQ poll + per-row agent invoke
+ accumulate per-channel counts. Cloud Run Job semantics (exit 0 vs
non-zero) come from whether the tick saw any unrecoverable errors.
Cloud Scheduler retries on its own; we do not loop or sleep here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..agents.base import BaseAgent
from ..common.audit_log import AuditLogClient
from ..common.memory_bank import MemoryBank
from .channels.chat import ChatRejectionError, ChatSendError, ChatWebhookClient
from .channels.gmail import (
    GmailDraftClient,
    GmailDraftError,
    GmailDraftRejectionError,
)
from .formatters import (
    RoutingMessageContext,
    format_chat_message,
    format_gmail_draft,
)
from .matrix import Channel, TriagedItem, route_item

log = logging.getLogger("agency_brain.routing.fanout")

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_HIGH_WINDOW_START = dt_time(9, 0)  # 09:00 local
DEFAULT_HIGH_WINDOW_END = dt_time(16, 0)  # 16:00 local (matrix: same_day_if_before_4pm)

# Channels managed by the routing fan-out worker today (ADR 0023, ADR 0032).
# Other matrix entries (MORNING_BRIEF, REST_OF_QUEUE, AIRTABLE_VIEW,
# GEMINI_INBOX) are intentionally not dispatched by this worker.
ACTIVE_CHANNELS: tuple[Channel, ...] = (
    Channel.GOOGLE_CHAT_DM,
    Channel.GMAIL_DRAFT,
)


# ---------------------------------------------------------------------------
# Inputs/outputs (AgentInput/AgentOutput protocols)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutInput:
    """One row from ``agent_outputs.triaged_items`` to dispatch."""

    aspects: list[str]  # AgentInput protocol — empty unless HIPAA-flagged upstream
    item: TriagedItem  # severity, owner, etc. (matrix.py contract)
    triaged_at: datetime
    message_context: RoutingMessageContext
    # Channels already dispatched for this item_id within the lookback
    # window (from polling SQL `routed_channels` column). Empty when
    # using the legacy single-channel polling mode.
    already_routed: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ChannelDispatchResult:
    """Per-channel dispatch outcome surfaced to the agent."""

    channel: Channel
    ok: bool
    status_code: int | None = None
    draft_id: str | None = None


@dataclass(frozen=True)
class FanoutOutput:
    """Per-row dispatch result with per-channel breakdown."""

    confidence: float = 1.0  # AgentOutput protocol — routing is rule-based
    dispatched_channels: tuple[str, ...] = ()
    skipped_channels: dict[str, str] = field(default_factory=dict)
    error_channels: dict[str, str] = field(default_factory=dict)

    @property
    def routed(self) -> bool:
        return bool(self.dispatched_channels)


# ---------------------------------------------------------------------------
# BQ surfaces
# ---------------------------------------------------------------------------


class BQQueryClient(Protocol):
    """Minimal BQ surface for poll + dispatch-record insert."""

    def query_rows(self, sql: str) -> list[dict[str, Any]]: ...

    def record_routing(
        self,
        *,
        item_id: str,
        channel: str,
        chat_status: int | None = None,
        agent_run_id: str | None = None,
    ) -> int:
        """Insert one row into ``agent_outputs.routed_events`` (ADR 0025).

        Returns the number of rows written (1 on success). Insert-only —
        BQ blocks DML on streaming-buffer rows for ~30 min after insert.
        """


# ---------------------------------------------------------------------------
# Channel adapters (Protocol + concrete wrappers)
# ---------------------------------------------------------------------------


class ChannelClient(Protocol):
    """A per-channel dispatcher.

    The agent calls :meth:`dispatch` and catches transient/permanent
    errors. Each adapter knows how to format its own payload from the
    :class:`RoutingMessageContext`; the agent stays channel-agnostic.
    """

    @property
    def channel(self) -> Channel: ...

    def dispatch(self, input: FanoutInput) -> ChannelDispatchResult: ...


class _ChatAdapter:
    """Wraps :class:`ChatWebhookClient` as a :class:`ChannelClient`."""

    channel = Channel.GOOGLE_CHAT_DM

    def __init__(self, chat_client: ChatWebhookClient) -> None:
        self._chat = chat_client

    def dispatch(self, input: FanoutInput) -> ChannelDispatchResult:
        text = format_chat_message(input.message_context)
        result = self._chat.send(text)  # may raise ChatSendError / ChatRejectionError
        return ChannelDispatchResult(
            channel=self.channel,
            ok=result.ok,
            status_code=result.status,
        )


class _GmailAdapter:
    """Wraps :class:`GmailDraftClient` as a :class:`ChannelClient` (ADR 0032)."""

    channel = Channel.GMAIL_DRAFT

    def __init__(
        self,
        gmail_client: GmailDraftClient,
        *,
        recipient_email: str,
    ) -> None:
        if not recipient_email:
            raise ValueError("recipient_email must be non-empty")
        self._gmail = gmail_client
        self._recipient = recipient_email

    def dispatch(self, input: FanoutInput) -> ChannelDispatchResult:
        subject, body_markdown = format_gmail_draft(input.message_context)
        thread_id = input.message_context.gmail_thread_id
        # Threading is source-gated: only Gmail signals can land in a
        # thread, even when an upstream stamps a thread_id by mistake
        # for a non-Gmail source.
        if thread_id and input.message_context.source != "gmail":
            thread_id = None
        result = self._gmail.draft(
            recipient_email=self._recipient,
            subject=subject,
            body_markdown=body_markdown,
            thread_id=thread_id,
        )
        return ChannelDispatchResult(
            channel=self.channel,
            ok=result.ok,
            draft_id=result.draft_id,
        )


def make_chat_adapter(chat_client: ChatWebhookClient) -> ChannelClient:
    return _ChatAdapter(chat_client)


def make_gmail_adapter(gmail_client: GmailDraftClient, *, recipient_email: str) -> ChannelClient:
    return _GmailAdapter(gmail_client, recipient_email=recipient_email)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class RoutingFanoutAgent(BaseAgent[FanoutInput, FanoutOutput]):
    """Per-row multi-channel dispatch (ADR 0023, ADR 0032).

    Walks the routing matrix's leadership intents for the row, skips
    channels without a registered adapter (e.g. MORNING_BRIEF —
    handled by a different agent), skips channels already in
    ``input.already_routed``, and dispatches the rest. Per-channel
    errors do not block other channels; the agent emits one audit row
    summarizing the overall outcome.
    """

    def __init__(
        self,
        *,
        sa_email: str,
        audit_log: AuditLogClient,
        memory_bank: MemoryBank,
        channel_clients: dict[Channel, ChannelClient],
        bq: BQQueryClient,
        timezone: str = DEFAULT_TIMEZONE,
        high_window_start: dt_time = DEFAULT_HIGH_WINDOW_START,
        high_window_end: dt_time = DEFAULT_HIGH_WINDOW_END,
        clock: Any = None,
    ) -> None:
        super().__init__(
            agent_id="routing-fanout",
            sa_email=sa_email,
            audit_log=audit_log,
            memory_bank=memory_bank,
        )
        if not channel_clients:
            raise ValueError("channel_clients must be non-empty")
        self._channels = dict(channel_clients)
        self._bq = bq
        self._tz = ZoneInfo(timezone)
        self._high_start = high_window_start
        self._high_end = high_window_end
        # ``clock`` is a no-arg callable returning a tz-aware ``datetime``;
        # tests override this to make severity-window logic deterministic.
        self._clock = clock or (lambda: datetime.now(UTC))

    def _run(self, input: FanoutInput) -> FanoutOutput:
        decision = route_item(input.item)
        intent_channels = [i.channel for i in decision.leadership_view.intents]

        dispatched: list[str] = []
        skipped: dict[str, str] = {}
        errors: dict[str, str] = {}

        for ch in intent_channels:
            adapter = self._channels.get(ch)
            if adapter is None:
                # MORNING_BRIEF, REST_OF_QUEUE, AIRTABLE_VIEW, GEMINI_INBOX
                # are matrix-routed but not dispatched by this worker.
                skipped[ch.value] = "no-adapter"
                continue
            if ch.value in input.already_routed:
                skipped[ch.value] = "already-routed"
                continue
            if not self._channel_window_allows(ch, input.item.severity):
                skipped[ch.value] = "severity-window-skip"
                continue

            try:
                result = adapter.dispatch(input)
            except (ChatRejectionError, GmailDraftRejectionError) as exc:
                # Permanent (4xx) — record and continue. Other channels
                # in the same row are unaffected. run_fanout_tick reads
                # error_channels and tags this for exit-code accounting.
                errors[ch.value] = f"permanent: {type(exc).__name__}: {exc}"
                continue
            except (ChatSendError, GmailDraftError) as exc:
                # Transient (5xx, network, timeout) — record and
                # continue. Cloud Scheduler retries the whole tick on
                # exit non-zero; the per-channel LEFT JOIN dedup means
                # the next tick re-attempts only the failed channel.
                errors[ch.value] = f"transient: {type(exc).__name__}: {exc}"
                continue
            except Exception as exc:
                errors[ch.value] = f"unexpected: {type(exc).__name__}: {exc}"
                continue

            try:
                self._bq.record_routing(
                    item_id=input.item.item_id,
                    channel=ch.value,
                    chat_status=result.status_code,
                )
            except Exception as exc:
                # Routing landed in the channel but we failed to record it.
                # No routed_events row exists, so the next tick's dedup will
                # NOT suppress this — it re-dispatches. This is at-most-twice
                # on a record_routing failure (an accepted trade vs dropping
                # the notification); it is NOT deduped away. Surfaced as
                # transient so the retry is expected.
                errors[ch.value] = (
                    f"transient: routed_events insert failed: " f"{type(exc).__name__}: {exc}"
                )
                continue
            dispatched.append(ch.value)

        return FanoutOutput(
            dispatched_channels=tuple(dispatched),
            skipped_channels=skipped,
            error_channels=errors,
        )

    def _channel_window_allows(self, channel: Channel, severity: str) -> bool:
        """Severity/time-window gate. Only Chat 'high' is gated.

        Critical bypasses on every channel. Gmail drafts always fire
        when matrix-routed (ADR 0032: drafts don't notify, so off-hours
        pile-up is acceptable). Other channels (Chat 'high') keep the
        09:00–16:00 PT window from ADR 0023.
        """
        sev = severity.lower()
        if sev == "critical":
            return True
        if channel == Channel.GMAIL_DRAFT:
            return True
        if channel != Channel.GOOGLE_CHAT_DM:
            return True
        # Chat-only window for non-critical severities.
        if sev != "high":
            # `medium`/`low`/`info` are filtered by the polling SQL but
            # we double-guard here for orchestrator unit tests.
            return False
        local_now = self._clock().astimezone(self._tz).time()
        return self._high_start <= local_now < self._high_end

    def _summarize_input(self, input: FanoutInput) -> str | None:
        return (
            f"item_id={input.item.item_id} severity={input.item.severity} "
            f"source={input.message_context.source}"
        )

    def _summarize_output(self, output: FanoutOutput) -> str | None:
        parts: list[str] = []
        if output.dispatched_channels:
            parts.append(f"dispatched={','.join(output.dispatched_channels)}")
        if output.skipped_channels:
            parts.append(
                "skipped="
                + ",".join(
                    f"{ch}({reason})" for ch, reason in sorted(output.skipped_channels.items())
                )
            )
        if output.error_channels:
            parts.append(
                "errors="
                + ",".join(
                    f"{ch}({err.split(':', 1)[0]})"
                    for ch, err in sorted(output.error_channels.items())
                )
            )
        return " ".join(parts) if parts else "no-op"


# Backwards-compatible alias for one transitional release. Tests +
# entrypoint should migrate to RoutingFanoutAgent directly.
ChatFanoutAgent = RoutingFanoutAgent


# ---------------------------------------------------------------------------
# Tick driver
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    """Aggregate outcome of one fan-out tick."""

    polled: int = 0
    dispatched_per_channel: dict[str, int] = field(default_factory=dict)
    skipped_per_channel: dict[str, int] = field(default_factory=dict)
    transient_errors: list[str] = field(default_factory=list)
    permanent_errors: list[str] = field(default_factory=list)

    @property
    def dispatched(self) -> int:
        return sum(self.dispatched_per_channel.values())

    @property
    def skipped(self) -> int:
        return sum(self.skipped_per_channel.values())


def run_fanout_tick(
    *,
    bq: BQQueryClient,
    agent: RoutingFanoutAgent,
    poll_sql: str,
    row_to_input: Any,
) -> TickResult:
    """Execute one fan-out tick.

    ``row_to_input`` is a callable that converts a BQ row dict into a
    :class:`FanoutInput`. Production wiring lives in the Cloud Run
    Job entrypoint; tests inject a stub.

    Returns a :class:`TickResult` summarizing what happened. Caller
    decides exit code: any ``transient_errors`` ⇒ exit non-zero so
    Cloud Scheduler logs it; ``permanent_errors`` alone ⇒ exit zero
    (the rows are dead and re-running won't help).
    """
    rows = bq.query_rows(poll_sql)
    result = TickResult(polled=len(rows))
    for row in rows:
        try:
            agent_input = row_to_input(row)
        except Exception as exc:
            result.permanent_errors.append(
                f"row_to_input failed for {row.get('item_id', '?')}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        try:
            output = agent.invoke(agent_input)
        except Exception as exc:
            # _run catches per-channel errors and surfaces them via
            # output.error_channels — anything that bubbles out is an
            # unexpected failure. BaseAgent.invoke already emitted a
            # failure audit row.
            result.transient_errors.append(
                f"unexpected error {agent_input.item.item_id}: " f"{type(exc).__name__}: {exc}"
            )
            continue

        for ch in output.dispatched_channels:
            result.dispatched_per_channel[ch] = result.dispatched_per_channel.get(ch, 0) + 1
        for ch in output.skipped_channels:
            result.skipped_per_channel[ch] = result.skipped_per_channel.get(ch, 0) + 1
        for ch, err in output.error_channels.items():
            tagged = f"{agent_input.item.item_id} {ch}: {err}"
            if err.startswith("permanent:"):
                result.permanent_errors.append(tagged)
            else:
                result.transient_errors.append(tagged)

    return result
