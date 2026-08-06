"""BigQuery + Calendar loaders for the Risk Watcher.

The package replaces the prior single-file ``loaders.py`` (ADR 0034
§3). Public imports stay stable: callers continue to write
``from ...loaders import EcommerceClientStateLoader,
RiskProfileThresholdsLoader``.

- ``thresholds`` — shared ``RiskProfileThresholdsLoader``; reads
  ``airtable_replica.risk_profiles`` per segment.
- ``ecommerce`` — ``EcommerceClientStateLoader`` (ADR 0033 PR-B).
- ``local_service`` — ``LocalServiceClientStateLoader`` (ADR 0034 PR-D).
- ``agency_partner`` — added in PR-E (ADR 0034 §5), intentionally
  empty until Vantage federation lands.
"""

from .agency_partner import AgencyPartnerClientStateLoader
from .common import BQQueryClient
from .ecommerce import EcommerceClientStateLoader
from .local_service import LocalServiceClientStateLoader
from .personal import PersonalClientStateLoader
from .thresholds import RiskProfileThresholdsLoader

__all__ = [
    "AgencyPartnerClientStateLoader",
    "BQQueryClient",
    "EcommerceClientStateLoader",
    "LocalServiceClientStateLoader",
    "PersonalClientStateLoader",
    "RiskProfileThresholdsLoader",
]
