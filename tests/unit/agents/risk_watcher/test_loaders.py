"""Unit tests for the BQ loaders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.risk_watcher.loaders import (
    EcommerceClientStateLoader,
    RiskProfileThresholdsLoader,
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


# ---------------------------------------------------------------------------
# RiskProfileThresholdsLoader
# ---------------------------------------------------------------------------


def test_thresholds_loader_keys_by_pattern_name() -> None:
    bq = _FakeBQ(
        canned={
            "risk_profiles": [
                {
                    "pattern_name": "Acknowledgment Gap",
                    "threshold_value": 5.0,
                    "threshold_unit": "business days",
                    "window": "rolling 7 days",
                },
                {
                    "pattern_name": "Silent After Deliverable",
                    "threshold_value": 5.0,
                    "threshold_unit": "business days",
                    "window": "since last delivery",
                },
            ]
        }
    )
    loader = RiskProfileThresholdsLoader(bq=bq, project_id="agency-brain-demo")

    out = loader.load(Segment.ECOMMERCE)

    assert set(out.keys()) == {"Acknowledgment Gap", "Silent After Deliverable"}
    assert out["Acknowledgment Gap"]["threshold_value"] == 5.0
    assert out["Acknowledgment Gap"]["threshold_unit"] == "business days"
    assert out["Acknowledgment Gap"]["window"] == "rolling 7 days"
    # SQL filter included the segment filter (enum value, not enum name).
    assert "'E-commerce'" in bq.captured[0]
    assert "active = TRUE" in bq.captured[0]


def test_thresholds_loader_returns_empty_when_no_active_rows() -> None:
    bq = _FakeBQ(canned={"risk_profiles": []})
    loader = RiskProfileThresholdsLoader(bq=bq, project_id="prj")
    assert loader.load(Segment.ECOMMERCE) == {}


# ---------------------------------------------------------------------------
# EcommerceClientStateLoader
# ---------------------------------------------------------------------------


def test_ecommerce_loader_returns_empty_when_no_accounts() -> None:
    bq = _FakeBQ(canned={"FROM `prj.airtable_replica.accounts` a": []})
    loader = EcommerceClientStateLoader(bq=bq, project_id="prj")
    assert loader.load() == ()


def test_ecommerce_loader_assembles_state_from_three_queries() -> None:
    """One account, with pending tasks + a deliverable + recent inbound."""
    bq = _FakeBQ(
        canned={
            # Accounts query — match on accounts table FROM clause
            "FROM `prj.airtable_replica.accounts` a": [
                {
                    "account_id": "recAcct1",
                    "company_name": "Acme E-comm",
                    "primary_project_id": "recProj1",
                }
            ],
            # Pending drafted tasks query
            "approval_status = 'Drafted by Agent'": [
                {
                    "record_id": "recT_old",
                    "task_name": "Follow up",
                    "created": datetime(2026, 4, 27, 9, tzinfo=UTC),
                    "account_id": "recAcct1",
                }
            ],
            # Last completed deliverable query
            "MAX(t.completed_date)": [
                {
                    "account_id": "recAcct1",
                    "last_completed_at": datetime(2026, 5, 5, tzinfo=UTC),
                }
            ],
            # Last inbound triage query — keyed off the
            # `t.source = 'Triage Agent'` filter that distinguishes it
            # from the deliverables / pending-drafts queries.
            "t.source = 'Triage Agent'": [
                {
                    "account_id": "recAcct1",
                    "last_inbound_at": datetime(2026, 5, 1, tzinfo=UTC),
                }
            ],
        }
    )
    loader = EcommerceClientStateLoader(bq=bq, project_id="prj")

    states = loader.load()

    assert len(states) == 1
    s = states[0]
    assert s.account_id == "recAcct1"
    assert s.account_name == "Acme E-comm"
    assert s.segment == Segment.ECOMMERCE
    assert s.project_id == "recProj1"
    pending = s.extras["pending_drafted_tasks"]
    assert len(pending) == 1
    assert pending[0]["record_id"] == "recT_old"
    assert s.extras["last_completed_deliverable_at"] == datetime(2026, 5, 5, tzinfo=UTC)
    assert s.extras["last_inbound_at"] == datetime(2026, 5, 1, tzinfo=UTC)


def test_ecommerce_loader_handles_account_with_no_pending_or_inbound() -> None:
    """Account exists but writer + reader return zero rows for its id."""
    bq = _FakeBQ(
        canned={
            "FROM `prj.airtable_replica.accounts` a": [
                {
                    "account_id": "recAcctNew",
                    "company_name": "New E-comm",
                    "primary_project_id": None,
                }
            ],
            # All other queries return empty (default behavior).
        }
    )
    loader = EcommerceClientStateLoader(bq=bq, project_id="prj")
    states = loader.load()

    assert len(states) == 1
    s = states[0]
    assert s.extras["pending_drafted_tasks"] == []
    assert s.extras["last_completed_deliverable_at"] is None
    assert s.extras["last_inbound_at"] is None


def test_accounts_query_filters_to_ecommerce_active_non_hipaa() -> None:
    bq = _FakeBQ(canned={"FROM `prj.airtable_replica.accounts` a": []})
    loader = EcommerceClientStateLoader(bq=bq, project_id="prj")
    loader.load()

    sql = bq.captured[0]
    assert "segment = 'E-commerce'" in sql
    assert "status IN ('Active', 'Mature')" in sql
    assert "hipaa = FALSE" in sql
