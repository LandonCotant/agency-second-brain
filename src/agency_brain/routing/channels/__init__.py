"""Per-channel delivery adapters for WS-D fan-out.

Each adapter implements a narrow client surface (`send`/`draft`,
error classes). The orchestrator in `routing/fanout.py` calls them.

Chat lands first (ADR 0023). Gmail (drafts) is the second channel
(ADR 0032). Airtable and Gemini Inbox follow as separate modules
with the same shape.
"""

from .chat import (
    ChatPostResult,
    ChatRejectionError,
    ChatSendError,
    ChatWebhookClient,
    HttpPoster,
)
from .gmail import (
    GmailDraftClient,
    GmailDraftError,
    GmailDraftRejectionError,
    GmailDraftResult,
    GmailServiceFactory,
)

__all__ = [
    "ChatPostResult",
    "ChatRejectionError",
    "ChatSendError",
    "ChatWebhookClient",
    "GmailDraftClient",
    "GmailDraftError",
    "GmailDraftRejectionError",
    "GmailDraftResult",
    "GmailServiceFactory",
    "HttpPoster",
]
