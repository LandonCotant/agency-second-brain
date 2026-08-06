"""Unit tests for ``AgencyPartnerClientStateLoader`` (ADR 0034 §5)."""

from __future__ import annotations

from typing import Any

from agency_brain.agents.risk_watcher.loaders import (
    AgencyPartnerClientStateLoader,
)


class _FakeBQ:
    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        del sql
        # Should never be called — the segment-loop's skip-if-empty-signals
        # branch short-circuits before the loader runs. This stub
        # asserts that contract by raising if it fires.
        raise AssertionError(
            "AgencyPartnerClientStateLoader.load() should not query BQ "
            "until Vantage signals are wired (ADR 0034 §5)."
        )


def test_load_returns_empty_until_vantage_lands() -> None:
    """Records the deliberate skeleton shape.

    A future PR that adds Vantage-fed signals MUST consciously change
    this test alongside adding the queries — the test assertion is a
    deliberate tripwire so the empty-loader behavior can't drift
    silently.
    """
    loader = AgencyPartnerClientStateLoader(bq=_FakeBQ(), project_id="prj")
    assert loader.load() == ()
