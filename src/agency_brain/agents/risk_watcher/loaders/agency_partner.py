"""Agency Partner client-state loader (ADR 0034 §5 — skeleton).

All five spec'd Agency Partner signals (Raw Data Inquiry, Refinement
Silence, Report Volume Decline, Refinement Ratio Drop, end-client-
roster Acknowledgment Gap) require Vantage federation tables that
aren't yet provisioned (WS-B PR-4 deferred). Until then this loader
is intentionally a no-op — the multi-segment loop in `main.py` is
structurally complete, but `agency_partner_profile.build` returns
`Profile(signals=())` and the loop's skip-if-empty-signals branch
never actually calls `load()`.

When Vantage federation lands, the body of `load()` will fill in the
queries that today live as inline comments below — no orchestration
or wiring changes required at the call site.
"""

from __future__ import annotations

from ..models import ClientState
from .common import BQQueryClient


class AgencyPartnerClientStateLoader:
    """Empty until Vantage tables ship.

    Post-Vantage scope (deletion-of-comments pattern):
    - SELECT active Agency Partner accounts from `airtable_replica.accounts`.
    - JOIN per-account end-client roster from `vantage_replica.end_clients`.
    - JOIN report-access counts (60d window) from `vantage_replica.report_access`.
    - JOIN refinement-question counts from `vantage_replica.refinement_qs`.
    - JOIN raw-data-inquiry classifier output from `agent_outputs.triaged_items`
      where `pattern` ∈ {raw_data_request}.
    - Compute baseline ratios in a CTE (refinement Q : report ratio,
      end-client count delta vs 60d baseline) — ADR 0034 §6 BQ-per-tick
      strategy.
    """

    def __init__(self, *, bq: BQQueryClient, project_id: str) -> None:
        self._bq = bq
        self._project_id = project_id

    def load(self) -> tuple[ClientState, ...]:
        return ()
