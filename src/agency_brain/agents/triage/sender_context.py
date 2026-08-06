"""Sender-contact context loader for the Triage prompt.

Looks up the sender's email in airtable_replica.contacts and returns
a compact text block with warmth, relationship type, last contact,
next followup, and account association. Injected into the prompt as
{{sender_context}} so the classifier can weight severity and action
type based on relationship signals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# The sender email comes from the inbound message's From: header —
# attacker-controlled. The BQQueryClient protocol is string-only (no query
# params), so the only safe way to inline it is a strict allowlist: reject
# anything outside ordinary address characters. Escaping is NOT sufficient
# here — quote-escaping without backslash-escaping lets `\'` close the
# string literal early. No legitimate Contacts row contains these
# characters, so rejecting means "sender not found", which is correct.
_EMAIL_SAFE_RE = re.compile(r"^[A-Za-z0-9.@_+\-]+$")


class BQQueryClient(Protocol):
    def query_rows(self, sql: str) -> list[dict]: ...


@dataclass(frozen=True)
class SenderContactRow:
    name: str
    email: str
    warmth: str | None
    relationship_type: str | None
    last_contact: str | None
    next_followup: str | None
    account_name: str | None
    segment: str | None


_SENDER_LOOKUP_SQL = """
SELECT
  c.name,
  c.email,
  c.warmth,
  c.relationship_type,
  CAST(c.last_contact AS STRING) AS last_contact,
  CAST(c.next_followup AS STRING) AS next_followup,
  a.company_name AS account_name,
  a.segment
FROM `{project}.airtable_replica.contacts` c
LEFT JOIN `{project}.airtable_replica.accounts` a
  ON c.account[SAFE_OFFSET(0)] = a._airtable_record_id
WHERE LOWER(c.email) = LOWER('{sender_email}')
  AND (c.hipaa_excluded IS NULL OR c.hipaa_excluded = FALSE)
  AND (a.hipaa_excluded IS NULL OR a.hipaa_excluded = FALSE)
ORDER BY c._airtable_last_modified DESC
LIMIT 1
"""


class SenderContactLoader:
    """Loads contact context for a sender email from BQ.

    Follows the same pattern as GoalContextLoader and
    AccountOwnersContextLoader — implements a text_block() method
    that returns a string suitable for prompt injection.
    """

    def __init__(self, *, bq_client: BQQueryClient, project_id: str) -> None:
        self._bq = bq_client
        self._project_id = project_id

    def load(self, sender_email: str) -> SenderContactRow | None:
        if not sender_email or "@" not in sender_email:
            return None
        if not _EMAIL_SAFE_RE.match(sender_email):
            return None
        sql = _SENDER_LOOKUP_SQL.format(project=self._project_id, sender_email=sender_email)
        rows = self._bq.query_rows(sql)
        if not rows:
            return None
        r = rows[0]
        return SenderContactRow(
            name=r.get("name", ""),
            email=r.get("email", ""),
            warmth=r.get("warmth"),
            relationship_type=r.get("relationship_type"),
            last_contact=r.get("last_contact"),
            next_followup=r.get("next_followup"),
            account_name=r.get("account_name"),
            segment=r.get("segment"),
        )

    def text_block(self, sender_email: str) -> str:
        row = self.load(sender_email)
        if row is None:
            return "(sender not found in Contacts)"
        lines = [
            f"Name: {row.name}",
            f"Warmth: {row.warmth or 'unknown'}",
            f"Relationship: {row.relationship_type or 'unknown'}",
        ]
        if row.last_contact:
            lines.append(f"Last contact: {row.last_contact}")
        if row.next_followup:
            lines.append(f"Next followup due: {row.next_followup}")
        if row.account_name:
            lines.append(f"Account: {row.account_name} ({row.segment or 'unknown segment'})")
        return "\n".join(lines)
