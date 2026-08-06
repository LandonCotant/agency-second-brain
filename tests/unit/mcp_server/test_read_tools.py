"""Unit tests for MCP read-tool SQL shape + response framing.

Focuses on the structured tools (``get_calendar_events``,
``open_risk_flags``, ``client_summary``). ``brain_ask`` is exercised
through the Retriever's own test suite at
``tests/unit/agents/knowledge_surfacer/test_retriever.py``.
"""

from __future__ import annotations

import pytest
from agency_brain.mcp_server.tools import read as read_tools


class _FakeBQ:
    """Captures issued SQL + returns fixture rows per matcher."""

    def __init__(self, fixtures: list[tuple[str, list[dict]]] | None = None) -> None:
        self._fixtures = fixtures or []
        self.calls: list[tuple[str, list[dict]]] = []

    def __call__(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.calls.append((sql, parameters or []))
        for substr, rows in self._fixtures:
            if substr in sql:
                return rows
        return []


@pytest.fixture(autouse=True)
def _patch_query_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBQ()
    monkeypatch.setattr(read_tools, "query_rows", fake)
    read_tools._test_bq = fake  # type: ignore[attr-defined]


# ------------------------------ open_risk_flags -----------------------------


def test_open_risk_flags_hipaa_guard_is_in_where_and_join_is_inner() -> None:
    """The HIPAA filter MUST be a WHERE predicate on an INNER JOIN.

    With the old LEFT JOIN + ON-clause filter, a HIPAA account's flag was
    still emitted (with NULL company_name but live segment/reasoning) —
    the guard filtered nothing. INNER JOIN + WHERE also fails closed for
    flags whose account has no replica row at all, which is exactly the
    shape a HIPAA account takes (it never syncs)."""
    read_tools.open_risk_flags()
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, _ = fake.calls[0]
    assert "LEFT JOIN" not in sql
    on_clause = sql.split(" ON ", 1)[1].split(" WHERE ", 1)[0]
    where_clause = sql.split(" WHERE ", 1)[1]
    assert "hipaa_excluded" not in on_clause
    assert "COALESCE(a.hipaa_excluded, FALSE) = FALSE" in where_clause
    assert "rf.resolved_at IS NULL" in where_clause


def test_open_risk_flags_segment_and_severity_floor_params() -> None:
    read_tools.open_risk_flags(segment="E-commerce", min_severity="high")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, params = fake.calls[0]
    pdict = {p["name"]: p["value"] for p in params}
    assert pdict["segment"] == "E-commerce"
    assert sorted(pdict["severities"]) == ["critical", "high"]
    assert "rf.segment = @segment" in sql
    assert "LOWER(rf.severity) IN UNNEST(@severities)" in sql


def test_open_risk_flags_returns_structured_rows() -> None:
    from datetime import UTC, datetime

    fake = _FakeBQ(
        fixtures=[
            (
                "risk_flags",
                [
                    {
                        "flag_id": "rf-1",
                        "account_name": "Client A",
                        "segment": "Local Service",
                        "severity": "high",
                        "pattern_name": "no_contact_21d",
                        "flagged_at": datetime(2026, 6, 9, 14, 0, tzinfo=UTC),
                        "reasoning": "No inbound contact in 23 days.",
                    }
                ],
            )
        ]
    )
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(read_tools, "query_rows", fake)
    try:
        out = read_tools.open_risk_flags()
    finally:
        mp.undo()
    assert out["flags"][0]["flag_id"] == "rf-1"
    assert out["flags"][0]["account_name"] == "Client A"
    assert out["flags"][0]["reason"] == "No inbound contact in 23 days."
    assert out["flags"][0]["flagged_at"] == "2026-06-09T14:00:00+00:00"


# --------------------------- get_calendar_events ----------------------------


def test_get_calendar_events_sql_carries_load_bearing_filters() -> None:
    """Date-range tool MUST keep the HIPAA + kind invariants."""
    read_tools.get_calendar_events(start_date="2026-05-14", end_date="2026-05-21")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, _ = fake.calls[0]
    assert "note_kind = 'calendar_event'" in sql
    assert "hipaa_isolated" in sql and "FALSE" in sql
    assert "event_metadata.start >= @start_date" in sql
    assert "event_metadata.start < @end_date" in sql
    # Cancelled events are filtered.
    assert "'cancelled'" in sql
    # Chronological order is the contract of this tool.
    assert "ORDER BY event_metadata.start ASC" in sql


def test_get_calendar_events_passes_params() -> None:
    read_tools.get_calendar_events(
        start_date="2026-05-14",
        end_date="2026-05-21",
        scope="personal",
        include_all_day=False,
    )
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    _, params = fake.calls[0]
    pdict = {p["name"]: p["value"] for p in params}
    assert pdict["start_date"] == "2026-05-14"
    assert pdict["end_date"] == "2026-05-21"
    assert pdict["scope"] == "personal"
    assert pdict["include_all_day"] is False


def test_get_calendar_events_work_alias_maps_to_agency() -> None:
    """The user's mental model uses "work" / "personal"; the schema uses
    "agency" / "personal" per ADR 0037. Map the alias at the tool boundary."""
    read_tools.get_calendar_events(start_date="2026-05-14", end_date="2026-05-21", scope="work")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    _, params = fake.calls[0]
    scope_param = next(p for p in params if p["name"] == "scope")
    assert scope_param["value"] == "agency"


def test_get_calendar_events_unscoped_passes_null() -> None:
    """Omitted scope binds NULL so the SQL clause is a no-op."""
    read_tools.get_calendar_events(start_date="2026-05-14", end_date="2026-05-21")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    _, params = fake.calls[0]
    scope_param = next(p for p in params if p["name"] == "scope")
    assert scope_param["value"] is None


def test_get_calendar_events_returns_structured_events() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        (
            "calendar_event",
            [
                {
                    "note_id": "cal-216f5349f6d6",
                    "filename": "Linear Algebra Final",
                    "source_drive_url": "https://calendar.google.com/event?eid=abc",
                    "scope": "personal",
                    "start_iso": "2026-05-14T19:00:00-07:00",
                    "end_iso": "2026-05-14T21:00:00-07:00",
                    "location": None,
                    "status": "confirmed",
                },
                {
                    "note_id": "cal-ef5b71bca0b1",
                    "filename": "Oma's birthday",
                    "source_drive_url": None,
                    "scope": "personal",
                    "start_iso": "2026-05-18",
                    "end_iso": "2026-05-19",
                    "location": None,
                    "status": "confirmed",
                },
            ],
        )
    ]
    result = read_tools.get_calendar_events(start_date="2026-05-14", end_date="2026-05-21")
    assert result["total"] == 2
    assert result["date_range"] == {"start": "2026-05-14", "end": "2026-05-21"}
    timed, all_day = result["events"]
    assert timed["title"] == "Linear Algebra Final"
    assert timed["all_day"] is False
    assert timed["start"] == "2026-05-14T19:00:00-07:00"
    assert all_day["title"] == "Oma's birthday"
    assert all_day["all_day"] is True
    assert all_day["start"] == "2026-05-18"


def test_get_calendar_events_empty_window_returns_zero() -> None:
    """No rows → empty events list + total=0, not an error."""
    result = read_tools.get_calendar_events(start_date="2026-05-14", end_date="2026-05-15")
    assert result["events"] == []
    assert result["total"] == 0


# --------------------------- related_notes (ADR 0053) -----------------------


def test_related_notes_sql_carries_join_and_hipaa_guard() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1")
    sql, _ = fake.calls[0]
    assert "notes_links" in sql
    assert "LEFT JOIN" in sql
    assert "agent_outputs.notes" in sql
    assert "hipaa_isolated" in sql
    # Source filter is the focal note.
    assert "nl.source_note_id = @src" in sql


def test_related_notes_no_link_type_filter_returns_both() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1")
    sql, params = fake.calls[0]
    # No link_types arg → no IN UNNEST clause.
    assert "@link_types" not in sql
    names = {p["name"] for p in params}
    assert "link_types" not in names


def test_related_notes_wikilink_filter_emits_in_unnest() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1", link_types=["wikilink"])
    sql, params = fake.calls[0]
    assert "nl.link_type IN UNNEST(@link_types)" in sql
    link_types_param = next(p for p in params if p["name"] == "link_types")
    assert link_types_param["value"] == ["wikilink"]


def test_related_notes_semantic_filter_includes_null_link_type_rows() -> None:
    """Pre-ADR-0053 rows have NULL link_type but are semantically
    'semantic'. Asking for 'semantic' should include them."""
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1", link_types=["semantic"])
    sql, _ = fake.calls[0]
    assert "nl.link_type IN UNNEST(@link_types) OR nl.link_type IS NULL" in sql


def test_related_notes_returns_structured_rows() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        (
            "notes_links",
            [
                {
                    "target_note_id": "note-target-1",
                    "target_filename": "Client A",
                    "target_url": "https://drive/...",
                    "similarity": 1.0,
                    "link_type": "wikilink",
                },
                {
                    "target_note_id": "note-target-2",
                    "target_filename": "Peninsula Pricing",
                    "target_url": None,
                    "similarity": 0.83,
                    "link_type": "semantic",
                },
            ],
        )
    ]
    result = read_tools.related_notes(note_id="note-1")
    assert result["source_note_id"] == "note-1"
    assert result["total"] == 2
    first, second = result["links"]
    assert first["target_note_id"] == "note-target-1"
    assert first["link_type"] == "wikilink"
    assert first["similarity"] == 1.0
    assert second["link_type"] == "semantic"
    assert second["target_url"] is None


def test_related_notes_limit_clamps_to_100() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1", limit=999)
    _, params = fake.calls[0]
    limit_param = next(p for p in params if p["name"] == "limit")
    assert limit_param["value"] == 100


def test_related_notes_limit_floor_is_1() -> None:
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    read_tools.related_notes(note_id="note-1", limit=0)
    _, params = fake.calls[0]
    limit_param = next(p for p in params if p["name"] == "limit")
    assert limit_param["value"] == 1


# ----------------------------- open_drafts ---------------------------------


def _patch_open_drafts_fixture(rows: list[dict]) -> _FakeBQ:
    fake = _FakeBQ(fixtures=[("airtable_replica.tasks", rows)])
    read_tools.query_rows = fake  # type: ignore[assignment]
    read_tools._test_bq = fake  # type: ignore[attr-defined]
    return fake


def test_open_drafts_filters_to_drafted_by_agent_status() -> None:
    import datetime as dt

    _patch_open_drafts_fixture(
        [
            {
                "task_id": "recA",
                "task_name": "Reply to Client A about contract renewal",
                "category": "Client work",
                "action_type": "Reply",
                "task_type": "email",
                "owner": "owner@example.com",
                "source": "gmail",
                "source_reference": "thread/abc123",
                "due_date": dt.date(2026, 5, 22),
                "created": dt.datetime(2026, 5, 19, 14, 0, tzinfo=dt.UTC),
            }
        ]
    )
    out = read_tools.open_drafts()
    assert out["total"] == 1
    draft = out["drafts"][0]
    assert draft["task_id"] == "recA"
    assert draft["task_name"] == "Reply to Client A about contract renewal"
    assert draft["due_date"] == "2026-05-22"
    assert draft["created"].startswith("2026-05-19T14:00:00")
    # SQL load-bearing invariants
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, _ = fake.calls[0]
    assert "approval_status = 'Drafted by Agent'" in sql
    assert "hipaa_excluded" in sql  # via exclude_hipaa()
    assert "ORDER BY _airtable_last_modified DESC" in sql


def test_open_drafts_empty_returns_empty_list() -> None:
    _patch_open_drafts_fixture([])
    out = read_tools.open_drafts()
    assert out == {"drafts": [], "total": 0}


def test_open_drafts_limit_clamped() -> None:
    fake = _patch_open_drafts_fixture([])
    read_tools.open_drafts(limit=500)
    _, params = fake.calls[0]
    limit_param = next(p for p in params if p["name"] == "limit")
    assert limit_param["value"] == 100
    # Floor test
    read_tools.open_drafts(limit=0)
    _, params = fake.calls[1]
    limit_param = next(p for p in params if p["name"] == "limit")
    assert limit_param["value"] == 1


def test_open_drafts_handles_null_optional_fields() -> None:
    _patch_open_drafts_fixture(
        [
            {
                "task_id": "recB",
                "task_name": "Task with no due date",
                "category": None,
                "action_type": None,
                "task_type": None,
                "owner": None,
                "source": None,
                "source_reference": None,
                "due_date": None,
                "created": None,
            }
        ]
    )
    out = read_tools.open_drafts()
    draft = out["drafts"][0]
    assert draft["due_date"] is None
    assert draft["created"] is None
    assert draft["owner"] is None


# ------------------------------ brain_ask (ADR 0068) ------------------------


def test_brain_ask_runs_hybrid_and_surfaces_match_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """brain_ask wires cfg.hybrid_enabled/rrf_k into the Retriever and
    passes match_type + rrf_score through to the response (ADR 0068)."""
    from agency_brain.agents.knowledge_surfacer.retriever import DEFAULT_DIMS

    class _FakeEmbedder:
        def embed(self, *, text: str, model: str) -> list[float]:
            return [0.1] * DEFAULT_DIMS

    captured: dict[str, object] = {}

    def _fake_query_rows(sql: str, parameters: list[dict] | None = None) -> list[dict]:
        captured["sql"] = sql
        captured["params"] = parameters or []
        return [
            {
                "note_id": "n1",
                "filename": "birthdays.md",
                "source_drive_url": None,
                "markdown_content": "Jane Doe — birthday March 3",
                "scope": "personal",
                "note_kind": "area",
                "hipaa_isolated": False,
                "event_metadata_json": None,
                "distance": None,  # keyword-only fused row
                "match_type": "keyword",
                "rrf_score": 0.016393,
            }
        ]

    monkeypatch.setattr(read_tools, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(read_tools, "query_rows", _fake_query_rows)

    out = read_tools.brain_ask(query="Jane birthday")

    # Hybrid is the default → the keyword arm is in the issued SQL.
    sql = captured["sql"]
    assert isinstance(sql, str) and "SEARCH(markdown_content, @kw0)" in sql
    param_names = {p["name"] for p in captured["params"]}  # type: ignore[union-attr]
    assert {"kw0", "rrf_k"} <= param_names

    chunk = out["chunks"][0]
    assert chunk["match_type"] == "keyword"
    assert chunk["similarity"] == 0.0  # keyword-only → no semantic score
    assert chunk["rrf_score"] == 0.016393


# ------------------------------ open_commitments (ADR 0069) -----------------


def test_open_commitments_sql_shape_and_guards() -> None:
    """SQL must use effective-due fallback, HIPAA guards on both joins, and
    the days_overdue param."""
    read_tools.open_commitments(days_overdue=3)
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, params = fake.calls[0]
    # Effective due = explicit due_date OR extracted_at + stale days.
    assert "COALESCE(c.due_date, DATE(c.extracted_at)" in sql
    # HIPAA guard on the source-note join AND the account join.
    assert "COALESCE(n.hipaa_isolated, FALSE) = FALSE" in sql
    assert "COALESCE(a.hipaa_excluded, FALSE) = FALSE" in sql
    # Overdue threshold is parameterized.
    assert "DATE_SUB(CURRENT_DATE(), INTERVAL @days_overdue DAY)" in sql
    pdict = {p["name"]: p["value"] for p in params}
    assert pdict["days_overdue"] == 3
    # No direction filter unless asked.
    assert "c.direction = @direction" not in sql


def test_open_commitments_direction_and_account_filters() -> None:
    read_tools.open_commitments(direction="mine", account_name="Acme")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, params = fake.calls[0]
    pdict = {p["name"]: p["value"] for p in params}
    assert "c.direction = @direction" in sql
    assert pdict["direction"] == "mine"
    assert "LOWER(a.company_name) LIKE @acct" in sql
    assert pdict["acct"] == "%acme%"


def test_open_commitments_invalid_direction_ignored() -> None:
    read_tools.open_commitments(direction="garbage")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, _ = fake.calls[0]
    assert "c.direction = @direction" not in sql


def test_open_commitments_response_shape(monkeypatch) -> None:
    from datetime import date

    def _fake_query_rows(sql, parameters=None):
        return [
            {
                "commitment_id": "c1",
                "direction": "theirs",
                "counterparty": "Tim ClientA",
                "account_name": "Client A",
                "commitment_text": "send signed docs",
                "due_date": date(2026, 6, 10),
                "effective_due": date(2026, 6, 10),
                "days_overdue": 3,
                "source_url": "https://drive/x",
                "confidence": 0.8123,
            }
        ]

    monkeypatch.setattr(read_tools, "query_rows", _fake_query_rows)
    out = read_tools.open_commitments()
    c = out["commitments"][0]
    assert c["direction"] == "theirs"
    assert c["counterparty"] == "Tim ClientA"
    assert c["account"] == "Client A"
    assert c["what"] == "send signed docs"
    assert c["due_date"] == "2026-06-10"
    assert c["effective_due"] == "2026-06-10"
    assert c["days_overdue"] == 3
    assert c["confidence"] == 0.812


# ------------------------------ entity_facts (ADR 0070) ---------------------


def test_entity_facts_sql_uses_readtime_validity_and_guards() -> None:
    """SQL derives valid_to via LEAD (no UPDATE), guards HIPAA on both joins,
    and defaults to current-only (valid_to IS NULL)."""
    read_tools.entity_facts("Acme")
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, params = fake.calls[0]
    assert "LEAD(f.observed_date) OVER (" in sql
    assert "PARTITION BY COALESCE(f.entity_id, LOWER(f.entity_name)), f.predicate" in sql
    assert "COALESCE(n.hipaa_isolated, FALSE) = FALSE" in sql
    assert "COALESCE(a.hipaa_excluded, FALSE) = FALSE" in sql
    assert "WHERE valid_to IS NULL" in sql  # current-only by default
    pdict = {p["name"]: p["value"] for p in params}
    assert pdict["q"] == "%acme%"


def test_entity_facts_include_history_drops_current_filter() -> None:
    read_tools.entity_facts("Acme", include_history=True)
    fake: _FakeBQ = read_tools._test_bq  # type: ignore[attr-defined]
    sql, _ = fake.calls[0]
    assert "WHERE valid_to IS NULL" not in sql


def test_entity_facts_response_shape_and_current_flag(monkeypatch) -> None:
    from datetime import date

    def _fake_query_rows(sql, parameters=None):
        return [
            {
                "entity_name": "Acme",
                "predicate": "retainer",
                "value": "$3k/mo",
                "observed_date": date(2026, 5, 1),
                "valid_to": None,
                "confidence": 0.91,
                "source_url": "https://drive/x",
            },
            {
                "entity_name": "Acme",
                "predicate": "retainer",
                "value": "$2k/mo",
                "observed_date": date(2026, 1, 1),
                "valid_to": date(2026, 5, 1),
                "confidence": 0.88,
                "source_url": "https://drive/y",
            },
        ]

    monkeypatch.setattr(read_tools, "query_rows", _fake_query_rows)
    out = read_tools.entity_facts("Acme", include_history=True)
    cur, old = out["facts"]
    assert cur["value"] == "$3k/mo"
    assert cur["since"] == "2026-05-01"
    assert cur["valid_to"] is None
    assert cur["current"] is True
    assert cur["confidence"] == 0.91
    assert old["value"] == "$2k/mo"
    assert old["valid_to"] == "2026-05-01"
    assert old["current"] is False
