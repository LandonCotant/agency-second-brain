"""Domain-Wide Delegation (DWD) service factory.

Lifted from `agents/morning_brief/main.py` so both Morning Brief and
the WS-D routing fan-out (Gmail draft channel) share one factory
module — keeps the DWD allowlist surface (ADR 0027 / 0029) visible
in one place.

Usage:

    factory = DWDServiceFactory(
        target_principal="asb-agent-triage-sa@<project>.iam.gserviceaccount.com",
        scope="https://www.googleapis.com/auth/gmail.compose",
        api="gmail",
        api_version="v1",
    )
    service = factory.build(subject="owner@example.com")

`target_principal` is the SA the impersonating identity acts as (the
DWD-grantable SA per ADR 0027 §2). `subject` is the user whose
delegated credential is minted at `scope` — the recipient mailbox for
Gmail.compose, the calendar owner for Calendar.readonly, etc.
"""

from __future__ import annotations

from typing import Any


class DWDServiceFactory:
    """Builds a googleapiclient service via DWD impersonation.

    Scopes used by callers must already be on the DWD allowlist
    (`{gmail.compose, calendar.readonly}` per ADR 0029 §2). New scopes
    require an ADR + Workspace-admin DWD grant before a caller can
    instantiate this factory.
    """

    def __init__(
        self,
        *,
        target_principal: str,
        scope: str,
        api: str,
        api_version: str,
    ) -> None:
        self._target = target_principal
        self._scope = scope
        self._api = api
        self._api_version = api_version

    def build(self, subject: str) -> Any:
        from google.auth import default, impersonated_credentials
        from googleapiclient.discovery import build

        source, _ = default()
        # google-auth >= 2.16: `subject` is a constructor kwarg, not a
        # method. Older Google docs use `.with_subject()` on
        # service_account.Credentials (the key-file flow); for keyless
        # impersonation + DWD we pass subject= directly to
        # impersonated_credentials.Credentials.
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=self._target,
            target_scopes=[self._scope],
            subject=subject,
        )
        return build(
            self._api,
            self._api_version,
            credentials=creds,
            cache_discovery=False,
        )
