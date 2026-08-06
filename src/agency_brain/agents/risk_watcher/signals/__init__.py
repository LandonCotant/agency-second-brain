"""Risk Watcher signal classes.

Each class satisfies the ``Signal`` Protocol from
``risk_watcher.models``: a name, a severity, and a pure-function
``evaluate(client_state) -> Flag | None``. The watcher orchestrates;
signals reason.

- ``AcknowledgmentGapSignal`` — drafted-task overdue (E-commerce
  default; reused as Local Service "Approval Slowdown" via
  constructor params per ADR 0034 §2).
- ``SilentAfterDeliverableSignal`` — post-delivery silence
  (E-commerce, ADR 0033 PR-B).
- ``OwnerDisengagementSignal`` — Local Service owner-meeting recency
  (ADR 0034 §5).
"""

from .acknowledgment_gap import AcknowledgmentGapSignal
from .owner_disengagement import OwnerDisengagementSignal
from .personal_reengagement import PersonalReEngagementSignal
from .silent_after_deliverable import SilentAfterDeliverableSignal

__all__ = [
    "AcknowledgmentGapSignal",
    "OwnerDisengagementSignal",
    "PersonalReEngagementSignal",
    "SilentAfterDeliverableSignal",
]
