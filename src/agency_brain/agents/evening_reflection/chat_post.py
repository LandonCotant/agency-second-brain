"""Inline Chat-card poster for the Reflection Doc notification (ADR 0044).

Reuses ``routing.channels.chat.ChatWebhookClient`` directly — same
``second-brain-gchat-webhook`` secret, same payload shape — but routes
through this small helper instead of the routing fan-out so the
Reflection's daily-artifact dispatch doesn't mis-key
``routed_events.item_id`` (which is shaped for triage / risk-flag /
decision-source ids, not reflection_doc_id).

Failures are logged but never propagated. A Chat outage shouldn't kill
the daily Reflection Doc (the Doc itself is the artifact; the card is
just a nudge).
"""

from __future__ import annotations

import logging
from datetime import date

from ...routing.channels.chat import (
    ChatPostResult,
    ChatRejectionError,
    ChatSendError,
    ChatWebhookClient,
)

log = logging.getLogger("agency_brain.agents.evening_reflection.chat_post")


def post_reflection_card(
    *,
    chat: ChatWebhookClient,
    recipient_name: str,
    run_date: date,
    doc_url: str,
) -> ChatPostResult | None:
    """Post a single Chat card with a link to today's Reflection Doc.

    Returns the ``ChatPostResult`` on success, ``None`` on any failure
    (logged but not raised). The agent's ``_run`` does NOT depend on
    Chat success.
    """
    text = (
        f"Evening Reflection ready for {recipient_name} — "
        f"{run_date.strftime('%a, %b %d, %Y')}: {doc_url}"
    )
    try:
        return chat.send(text)
    except ChatRejectionError as exc:
        log.warning("evening_reflection.chat_rejected status=%s", exc.status)
    except ChatSendError:
        log.exception("evening_reflection.chat_send_failed")
    except Exception:
        log.exception("evening_reflection.chat_unexpected_error")
    return None
