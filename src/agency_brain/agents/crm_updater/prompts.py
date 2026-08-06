"""CRM Auto-updater prompt rendering (ADR 0047)."""

from __future__ import annotations

from ...common.prompts import load_prompt
from .models import GmailMessage

PROMPT_NAME = "crm_updater"
DEFAULT_PROMPT_VERSION = "v1"


def render_extraction_prompt(
    *,
    message: GmailMessage,
    known_account_names: tuple[str, ...] = (),
    known_contact_emails: tuple[str, ...] = (),
    version: str = DEFAULT_PROMPT_VERSION,
) -> str:
    """Render the extraction prompt for one email."""
    template = load_prompt(PROMPT_NAME, version)
    accounts_block = (
        "\n".join(f"- {n}" for n in known_account_names)
        if known_account_names
        else "(no known accounts loaded)"
    )
    contacts_block = (
        "\n".join(f"- {e}" for e in known_contact_emails)
        if known_contact_emails
        else "(no known contacts loaded)"
    )
    return (
        template.replace("{{subject}}", message.subject or "(no subject)")
        .replace("{{from}}", message.from_addr)
        .replace("{{to}}", ", ".join(message.to_addrs))
        .replace("{{cc}}", ", ".join(message.cc_addrs))
        .replace("{{body}}", message.body_text or "(empty body)")
        .replace("{{known_accounts}}", accounts_block)
        .replace("{{known_contacts}}", contacts_block)
    )
