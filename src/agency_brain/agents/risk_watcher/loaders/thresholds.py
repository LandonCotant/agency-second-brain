"""Per-segment threshold loader for the Risk Watcher.

Pulls per-pattern thresholds from ``airtable_replica.risk_profiles``
so tuning happens in Airtable, not code redeploys. Returns a dict
keyed by ``pattern_name`` carrying ``threshold_value`` (float),
``threshold_unit`` (str), and ``window`` (str). Callers — i.e. the
profile factories and signals — interpret unit/window themselves
because semantics differ per signal ("5 business days" vs "10%").
"""

from __future__ import annotations

from typing import Any

from ..models import Segment
from .common import BQQueryClient


class RiskProfileThresholdsLoader:
    """Loads thresholds for one segment per call."""

    def __init__(self, *, bq: BQQueryClient, project_id: str) -> None:
        self._bq = bq
        self._project_id = project_id

    def load(self, segment: Segment) -> dict[str, dict[str, Any]]:
        sql = (
            "SELECT pattern_name, threshold_value, threshold_unit, `window` "  # noqa: S608  enum-validated; no user input
            f"FROM `{self._project_id}.airtable_replica.risk_profiles` "
            "WHERE segment = @segment AND active = TRUE"
        ).replace("@segment", f"'{segment.value}'")
        # Inlined string substitution is safe because Segment is an
        # enum with whitelisted values (see models.Segment); no user
        # input flows here.
        rows = self._bq.query_rows(sql)
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            out[r["pattern_name"]] = {
                "threshold_value": float(r["threshold_value"]),
                "threshold_unit": r.get("threshold_unit"),
                "window": r.get("window"),
            }
        return out
