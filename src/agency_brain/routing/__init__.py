"""WS-D routing decision helpers and per-channel fan-out (ADR 0023, 0032).

`matrix.py` and `polling.py` are pure-function decision helpers. The
orchestrator (`fanout.py`), the channel adapters (`channels/*.py`), and
the formatters (`formatters.py`) carry side effects.
"""

from .channels import (
    ChatRejectionError,
    ChatSendError,
    ChatWebhookClient,
    GmailDraftClient,
    GmailDraftError,
    GmailDraftRejectionError,
)
from .fanout import (
    ACTIVE_CHANNELS,
    ChannelClient,
    ChannelDispatchResult,
    ChatFanoutAgent,
    FanoutInput,
    FanoutOutput,
    RoutingFanoutAgent,
    TickResult,
    make_chat_adapter,
    make_gmail_adapter,
    run_fanout_tick,
)
from .formatters import (
    ChatMessageContext,
    RoutingMessageContext,
    format_chat_message,
    format_gmail_draft,
)
from .matrix import (
    Channel,
    RouteIntent,
    RoutingDecision,
    RoutingView,
    TriagedItem,
    route_item,
)
from .polling import (
    build_decisions_poll_query,
    build_risk_flags_poll_query,
    build_triaged_items_poll_query,
)

__all__ = [
    "ACTIVE_CHANNELS",
    "Channel",
    "ChannelClient",
    "ChannelDispatchResult",
    "ChatFanoutAgent",
    "ChatMessageContext",
    "ChatRejectionError",
    "ChatSendError",
    "ChatWebhookClient",
    "FanoutInput",
    "FanoutOutput",
    "GmailDraftClient",
    "GmailDraftError",
    "GmailDraftRejectionError",
    "RouteIntent",
    "RoutingDecision",
    "RoutingFanoutAgent",
    "RoutingMessageContext",
    "RoutingView",
    "TickResult",
    "TriagedItem",
    "build_decisions_poll_query",
    "build_risk_flags_poll_query",
    "build_triaged_items_poll_query",
    "format_chat_message",
    "format_gmail_draft",
    "make_chat_adapter",
    "make_gmail_adapter",
    "route_item",
    "run_fanout_tick",
]
