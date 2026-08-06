"""Unit tests for ``PersonalClientStateLoader`` (ADR 0042)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agency_brain.agents.risk_watcher.loaders import (
    PersonalClientStateLoader,
)
from agency_brain.agents.risk_watcher.models import Segment


@dataclass
class _FakeBQ:
    """Returns canned responses keyed by SQL substring matches."""

    canned: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    captured: list[str] = field(default_factory=list)

    def query_rows(self, sql: str) -> list[dict]:
        self.captured.append(sql)
        for substring, rows in self.canned.items():
            if substring in sql:
                return rows
        return []


def _row(
    *,
    contact_id: str = "recContact1",
    contact_name: str | None = "Alex Mentor",
    relationship_type: str = "Mentor",
    warmth: str | None = "Warm",
    last_contact: date | None = date(2026, 4, 1),
    days_since_contact: int | None = 40,
    days_since_created: int = 365,
) -> dict[str, Any]:
    return {
        "contact_id": contact_id,
        "contact_name": contact_name,
        "relationship_type": relationship_type,
        "warmth": warmth,
        "last_contact": last_contact,
        "days_since_contact": days_since_contact,
        "days_since_created": days_since_created,
    }


def _loader(rows: list[dict[str, Any]]) -> PersonalClientStateLoader:
    fake_bq = _FakeBQ(canned={"airtable_replica.contacts": rows})
    return PersonalClientStateLoader(bq=fake_bq, project_id="brain-test")


def test_load_returns_one_state_per_row() -> None:
    rows = [
        _row(contact_id="recA", contact_name="Alex"),
        _row(contact_id="recB", contact_name="Beth", relationship_type="Friend"),
    ]
    states = _loader(rows).load()
    assert len(states) == 2
    assert states[0].account_id == "recA"
    assert states[0].account_name == "Alex"
    assert states[0].segment == Segment.PERSONAL
    assert states[1].account_id == "recB"


def test_load_returns_empty_tuple_when_no_rows() -> None:
    states = _loader([]).load()
    assert states == ()


def test_load_populates_extras_from_row() -> None:
    rows = [
        _row(
            relationship_type="Collaborator",
            warmth="Hot",
            days_since_contact=25,
            days_since_created=200,
        )
    ]
    states = _loader(rows).load()
    assert len(states) == 1
    extras = states[0].extras
    assert extras["relationship_type"] == "Collaborator"
    assert extras["warmth"] == "Hot"
    assert extras["days_since_contact"] == 25
    assert extras["days_since_created"] == 200
    assert extras["last_contact_at"] == date(2026, 4, 1)


def test_load_handles_null_warmth_and_null_last_contact() -> None:
    rows = [
        _row(
            warmth=None,
            last_contact=None,
            days_since_contact=None,
            days_since_created=180,
        )
    ]
    states = _loader(rows).load()
    extras = states[0].extras
    assert extras["warmth"] is None
    assert extras["last_contact_at"] is None
    assert extras["days_since_contact"] is None
    assert extras["days_since_created"] == 180


def test_load_falls_back_to_unnamed_when_contact_name_missing() -> None:
    rows = [_row(contact_name=None)]
    states = _loader(rows).load()
    assert states[0].account_name == "(unnamed contact)"


def test_load_sets_project_id_to_none_for_personal_contacts() -> None:
    """Personal contacts have no Project — project_id is always None."""
    states = _loader([_row()]).load()
    assert states[0].project_id is None


def test_sql_filters_on_relationship_type_not_null() -> None:
    """Loader-side gate per ADR 0042 §4."""
    loader = _loader([_row()])
    loader.load()
    assert len(loader._bq.captured) == 1  # type: ignore[attr-defined]
    sql = loader._bq.captured[0]  # type: ignore[attr-defined]
    assert "relationship_type IS NOT NULL" in sql
    assert "airtable_replica.contacts" in sql


def test_sql_pre_computes_days_since_contact_and_created() -> None:
    """The signal does no date math; the loader does it BQ-side."""
    loader = _loader([_row()])
    loader.load()
    sql = loader._bq.captured[0]  # type: ignore[attr-defined]
    assert "DATE_DIFF(CURRENT_DATE(), last_contact, DAY)" in sql
    assert "DATE_DIFF(CURRENT_DATE(), DATE(_airtable_last_modified), DAY)" in sql


def test_sql_uses_configured_project_id() -> None:
    loader = _loader([_row()])
    loader.load()
    sql = loader._bq.captured[0]  # type: ignore[attr-defined]
    assert "brain-test.airtable_replica.contacts" in sql
