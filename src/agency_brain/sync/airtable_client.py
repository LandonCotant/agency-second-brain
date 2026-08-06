"""Thin Airtable REST client used by the sync orchestrator.

The only public method is ``list_records``, which **requires** the caller to
pass a ``filter_formula``. There is no overload that omits it. Refusing to
issue a request without a formula is the load-bearing guard that keeps a
future caller from accidentally fetching the un-filtered base and
post-filtering in Python — which would violate PRD §4.1 layer 2.

Pagination is handled internally; the method yields one record dict at a time
in the shape the Airtable API returns
(``{"id": "rec...", "createdTime": "...", "fields": {...}}``).

Retry is limited to HTTP 429: Airtable's 5-req/sec/base limit returns 429
with a ``Retry-After`` header, and a single 429 mid-pagination would
otherwise fail the whole table for an hour (the next scheduled run can't
catch up inside that window). 429s are retried in place honoring
Retry-After, bounded by ``MAX_RATE_LIMIT_RETRIES``. All other non-2xx
responses still fail fast — there's nothing to mis-tune and the next run
picks up the same full pull.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    import requests

# Airtable REST base. Pinning the version protects against a future v1 → v2
# change shifting field semantics under us; bump deliberately when needed.
AIRTABLE_API_BASE = "https://api.airtable.com/v0"
AIRTABLE_PAGE_SIZE = 100  # API max

# 429 handling. Airtable enforces 5 req/sec/base and returns 429 with a
# Retry-After (typically 30s). Cap total retries so a sustained rate-limit
# can't hang the job past its run window.
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 30


class AirtableRequestError(RuntimeError):
    """Raised when Airtable returns a non-2xx response.

    Carries the HTTP status and response body so the operational log makes the
    failure mode obvious (rate limit, invalid PAT, unknown table, formula
    parse error, etc.).
    """

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Airtable HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


def _parse_retry_after(response: requests.Response) -> float:
    """Seconds to wait from a 429's ``Retry-After`` header, with a default."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS


class AirtableClient:
    def __init__(
        self,
        base_id: str,
        pat: str,
        session: Any = None,
        sleep: Any = None,
    ) -> None:
        # ``session`` is injected by tests so requests can be stubbed without
        # monkey-patching. Production callers omit it and a fresh
        # ``requests.Session`` is created on first use. ``sleep`` is injected
        # by tests so 429 backoff can be asserted without real waits.
        self._base_id = base_id
        self._pat = pat
        self._session = session
        self._sleep = sleep or time.sleep

    @property
    def base_id(self) -> str:
        return self._base_id

    def _http(self) -> requests.Session:
        if self._session is None:
            import requests as _requests

            self._session = _requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self._pat}"})
        return self._session

    def list_records(
        self,
        table_name: str,
        filter_formula: str,
        fields: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record matching ``filter_formula``.

        ``filter_formula`` must be a non-empty string. An empty formula is
        rejected with ``ValueError`` — the caller's responsibility is to pass
        ``"TRUE()"`` if they genuinely want no filter, which is an explicit
        decision and grep-able in code review.

        ``fields`` optionally restricts the columns returned (smaller payloads
        for the full-set ID pull used by the removal pass).
        """
        if not filter_formula:
            raise ValueError(
                f"list_records called for table {table_name!r} without a filter_formula. "
                "PRD §4.1 layer 2 requires HIPAA filtering at the source query — "
                "pass 'TRUE()' explicitly if you really mean no filter."
            )

        url = f"{AIRTABLE_API_BASE}/{self._base_id}/{quote(table_name, safe='')}"
        params: dict[str, Any] = {
            "pageSize": AIRTABLE_PAGE_SIZE,
            "filterByFormula": filter_formula,
        }
        if fields:
            # Airtable wants ``fields[]`` repeated for each entry; requests
            # serializes a list value into that shape automatically.
            params["fields[]"] = fields

        offset: str | None = None
        while True:
            if offset:
                params["offset"] = offset
            response = self._get_with_rate_limit_retry(url, params, table_name)
            payload = response.json()
            yield from payload.get("records", [])
            offset = payload.get("offset")
            if not offset:
                return

    def _get_with_rate_limit_retry(
        self, url: str, params: dict[str, Any], table_name: str
    ) -> requests.Response:
        """GET one page, retrying on HTTP 429 honoring ``Retry-After``.

        Retries the SAME page (offset is already in ``params``) so no records
        are skipped. Non-429 errors fail fast. Exhausting the retry budget
        raises the last 429 as an ``AirtableRequestError``.
        """
        attempts = 0
        while True:
            response = self._http().get(url, params=params, timeout=30)
            if response.status_code == 429 and attempts < MAX_RATE_LIMIT_RETRIES:
                attempts += 1
                retry_after = _parse_retry_after(response)
                self._sleep(retry_after)
                continue
            if response.status_code >= 400:
                raise AirtableRequestError(response.status_code, response.text)
            return response
