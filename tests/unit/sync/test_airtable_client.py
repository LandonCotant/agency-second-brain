"""Verify the Airtable client refuses to issue requests without a filter."""

from __future__ import annotations

import json

import pytest
from agency_brain.sync.airtable_client import (
    AirtableClient,
    AirtableRequestError,
)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class _RecordingSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict, timeout: int) -> _FakeResponse:
        self.calls.append((url, dict(params)))
        return self._responses.pop(0)


def test_list_records_without_filter_raises():
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=_RecordingSession([]))
    with pytest.raises(ValueError, match="filter_formula"):
        list(client.list_records("Clients", filter_formula=""))


def test_list_records_passes_filter_to_airtable_api():
    session = _RecordingSession([_FakeResponse(200, {"records": [{"id": "rec1", "fields": {}}]})])
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    records = list(client.list_records("Clients", filter_formula="NOT({HIPAA})"))

    assert len(records) == 1
    assert len(session.calls) == 1
    url, params = session.calls[0]
    assert url.endswith("/appTEST/Clients")
    assert params["filterByFormula"] == "NOT({HIPAA})"
    assert params["pageSize"] == 100


def test_list_records_paginates():
    session = _RecordingSession(
        [
            _FakeResponse(
                200,
                {"records": [{"id": "rec1", "fields": {}}], "offset": "tok1"},
            ),
            _FakeResponse(200, {"records": [{"id": "rec2", "fields": {}}]}),
        ]
    )
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    records = list(client.list_records("Clients", filter_formula="TRUE()"))

    assert [r["id"] for r in records] == ["rec1", "rec2"]
    assert session.calls[1][1]["offset"] == "tok1"


def test_list_records_url_encodes_table_name_with_spaces():
    session = _RecordingSession([_FakeResponse(200, {"records": []})])
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    list(client.list_records("Goal Scores", filter_formula="TRUE()"))

    url, _ = session.calls[0]
    assert url.endswith("/appTEST/Goal%20Scores")


def test_list_records_raises_on_http_error():
    session = _RecordingSession(
        [_FakeResponse(422, payload={"error": "INVALID_FORMULA"}, text='{"error":"x"}')]
    )
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    with pytest.raises(AirtableRequestError) as exc:
        list(client.list_records("Clients", filter_formula="TRUE()"))
    assert exc.value.status_code == 422


def test_list_records_retries_on_429_then_succeeds():
    """A 429 mid-pagination is retried in place honoring Retry-After,
    not surfaced as a table failure."""
    slept: list[float] = []
    session = _RecordingSession(
        [
            _FakeResponse(429, text="rate limited", headers={"Retry-After": "7"}),
            _FakeResponse(200, {"records": [{"id": "rec1", "fields": {}}]}),
        ]
    )
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session, sleep=slept.append)
    records = list(client.list_records("Clients", filter_formula="TRUE()"))
    assert [r["id"] for r in records] == ["rec1"]
    assert slept == [7.0]
    # The retried GET reused the same offset/params (page not skipped).
    assert len(session.calls) == 2


def test_list_records_gives_up_after_max_429_retries():
    from agency_brain.sync.airtable_client import MAX_RATE_LIMIT_RETRIES

    session = _RecordingSession(
        [_FakeResponse(429, text="rate limited") for _ in range(MAX_RATE_LIMIT_RETRIES + 1)]
    )
    client = AirtableClient(
        base_id="appTEST", pat="patFAKE", session=session, sleep=lambda _s: None
    )
    with pytest.raises(AirtableRequestError) as exc:
        list(client.list_records("Clients", filter_formula="TRUE()"))
    assert exc.value.status_code == 429
    assert len(session.calls) == MAX_RATE_LIMIT_RETRIES + 1


def test_retry_after_defaults_when_header_missing_or_bad():
    from agency_brain.sync.airtable_client import (
        DEFAULT_RETRY_AFTER_SECONDS,
        _parse_retry_after,
    )

    assert _parse_retry_after(_FakeResponse(429)) == DEFAULT_RETRY_AFTER_SECONDS
    assert (
        _parse_retry_after(_FakeResponse(429, headers={"Retry-After": "garbage"}))
        == DEFAULT_RETRY_AFTER_SECONDS
    )
    assert _parse_retry_after(_FakeResponse(429, headers={"Retry-After": "12"})) == 12.0


def test_fields_param_serialized_for_airtable():
    session = _RecordingSession([_FakeResponse(200, {"records": []})])
    client = AirtableClient(base_id="appTEST", pat="patFAKE", session=session)
    list(
        client.list_records(
            "Clients",
            filter_formula="TRUE()",
            fields=["Client Name", "HIPAA"],
        )
    )

    _, params = session.calls[0]
    assert params["fields[]"] == ["Client Name", "HIPAA"]
