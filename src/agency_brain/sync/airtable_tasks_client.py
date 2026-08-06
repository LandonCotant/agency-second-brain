"""Write-side Airtable client used by the Triage Agent's TaskDrafter (ADR 0019).

Separate from :mod:`airtable_client` (which is read-only and used by the
sync orchestrator) for two reasons:

1. **Different PAT, narrower scope.** The Triage Agent's PAT is scoped to
   Operations Tasks (write) + Projects (read for validation). The sync
   PAT is read-only on Operations + CRM. Splitting gives per-component
   blast radius if a PAT leaks.
2. **Different surface.** Sync needs ``list_records`` with a forced
   ``filterByFormula`` (PRD §4.1 layer 2 guard). The agent needs
   ``create_task`` and nothing else; smaller surface = fewer ways to
   misuse.

Implements the ``AirtableTasksClient`` Protocol declared in
:mod:`agency_brain.agents.triage.writers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    import requests

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
TASKS_TABLE_NAME = "Tasks"


class AirtableTasksWriteError(RuntimeError):
    """Raised when Airtable returns a non-2xx on a Tasks POST.

    Carries the HTTP status and response body so the operational log makes
    the failure mode obvious (rate limit, invalid PAT, schema rejection,
    required field missing, etc.).
    """

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Airtable Tasks POST HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class AirtableTasksWriteClient:
    """POST /v0/{base}/Tasks.

    Returns the created record id. Single public method; no list, no
    update, no delete — the agent never needs them and the smaller surface
    matches PRD §4.7's drafts-only boundary.
    """

    def __init__(
        self,
        base_id: str,
        pat: str,
        session: Any = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._base_id = base_id
        self._pat = pat
        self._session = session
        self._timeout = timeout

    @property
    def base_id(self) -> str:
        return self._base_id

    def _http(self) -> requests.Session:
        if self._session is None:
            import requests as _requests

            self._session = _requests.Session()
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self._pat}",
                    "Content-Type": "application/json",
                }
            )
        return self._session

    def create_task(self, fields: dict) -> str:
        """POST one row to the Tasks table; return the created record id.

        ``fields`` is the field-name → value mapping that
        :func:`writers.build_task_fields` produces. We pass it through
        verbatim under the ``{"fields": ...}`` envelope.
        """
        url = f"{AIRTABLE_API_BASE}/{self._base_id}/" f"{quote(TASKS_TABLE_NAME, safe='')}"
        response = self._http().post(
            url,
            json={"fields": fields, "typecast": False},
            timeout=self._timeout,
        )
        if response.status_code >= 400:
            raise AirtableTasksWriteError(response.status_code, response.text)
        payload = response.json()
        record_id = payload.get("id")
        if not record_id:
            raise AirtableTasksWriteError(
                response.status_code,
                f"Airtable response missing 'id': {response.text[:500]}",
            )
        return str(record_id)
