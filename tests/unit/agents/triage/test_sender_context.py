"""Unit tests for the sender-contact context loader."""

from __future__ import annotations

from agency_brain.agents.triage.sender_context import SenderContactLoader


class _StubBQ:
    def __init__(self, rows: list[dict]) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def query_rows(self, sql: str) -> list[dict]:
        self.queries.append(sql)
        return self._rows


def test_loader_returns_contact_block_with_warmth_and_account() -> None:
    bq = _StubBQ(
        [
            {
                "name": "Client A",
                "email": "tim@clientapi.com",
                "warmth": "Warm",
                "relationship_type": "Client",
                "last_contact": "2026-05-07",
                "next_followup": "2026-05-09",
                "account_name": "Client A",
                "segment": "Local Service",
            }
        ]
    )
    loader = SenderContactLoader(bq_client=bq, project_id="agency-brain-demo")
    block = loader.text_block("tim@clientapi.com")
    assert "Client A" in block
    assert "Warm" in block
    assert "Client" in block
    assert "2026-05-07" in block
    assert "2026-05-09" in block
    assert "Client A" in block
    assert "Local Service" in block


def test_loader_returns_not_found_for_unknown_sender() -> None:
    bq = _StubBQ([])
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    block = loader.text_block("unknown@example.com")
    assert "not found" in block.lower()


def test_loader_returns_not_found_for_empty_email() -> None:
    bq = _StubBQ([])
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    assert "not found" in loader.text_block("").lower()
    assert "not found" in loader.text_block("no-at-sign").lower()
    assert bq.queries == []


def test_loader_rejects_sql_metacharacters_without_querying() -> None:
    """From: is attacker-controlled; anything outside plain address chars
    must be rejected before SQL is built. Notably the backslash bypass:
    quote-escaping alone turns an input `\\'` into `\\\\'` — an escaped
    backslash followed by a LIVE quote that terminates the literal."""
    bq = _StubBQ([])
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    hostile = [
        "a\\' OR 1=1--@example.com",
        "a'or@example.com",
        'a"b@example.com',
        "a;DROP TABLE x@example.com",
        "a b@example.com",
        "a(@example.com",
        "a%0a@example.com" + "\n",
    ]
    for email in hostile:
        assert loader.load(email) is None, email
    assert bq.queries == []


def test_loader_accepts_ordinary_address_shapes() -> None:
    bq = _StubBQ([])
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    for email in ["a.b+tag@sub-domain.example.com", "x_y@example.co"]:
        loader.load(email)
    assert len(bq.queries) == 2


def test_sql_filters_hipaa_and_matches_email() -> None:
    bq = _StubBQ([])
    loader = SenderContactLoader(bq_client=bq, project_id="agency-brain-demo")
    loader.load("test@example.com")
    sql = bq.queries[0]
    assert "hipaa_excluded" in sql.lower()
    assert "test@example.com" in sql.lower()
    assert "airtable_replica.contacts" in sql
    assert "airtable_replica.accounts" in sql
    assert "LIMIT 1" in sql


def test_loader_handles_null_optional_fields() -> None:
    bq = _StubBQ(
        [
            {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "warmth": None,
                "relationship_type": None,
                "last_contact": None,
                "next_followup": None,
                "account_name": None,
                "segment": None,
            }
        ]
    )
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    block = loader.text_block("jane@example.com")
    assert "Jane Doe" in block
    assert "unknown" in block
    assert "Last contact" not in block
    assert "Next followup" not in block
    assert "Account" not in block


def test_load_returns_dataclass_with_correct_fields() -> None:
    bq = _StubBQ(
        [
            {
                "name": "Joe Roberts",
                "email": "joe@jroillustrations.com",
                "warmth": "Hot",
                "relationship_type": "Client",
                "last_contact": "2026-05-09",
                "next_followup": "2026-05-09",
                "account_name": "Client C Studio",
                "segment": "Local Service",
            }
        ]
    )
    loader = SenderContactLoader(bq_client=bq, project_id="p")
    row = loader.load("joe@jroillustrations.com")
    assert row is not None
    assert row.name == "Joe Roberts"
    assert row.warmth == "Hot"
    assert row.account_name == "Client C Studio"
