"""Owner Disengagement signal — fires when the agency owner has had
no meaningful engagement with the client across the threshold window.

ADR 0035 superseded the v1 calendar-only design. v2 reads a single
``last_engagement_at`` extras key that the loader computes as the
MAX of three sources: Calendar engagement events (exact-attendee-
email match against CRM contacts), Triage inbound activity
(``tasks.source = 'Triage Agent'`` → tasks.created), and approved-
or-done task modifications (``tasks.last_modified`` filtered to
``approval_status = 'Approved' OR status = 'Done'``).

Inputs from ``ClientState.extras``:

- ``last_engagement_at`` (datetime | None): MAX of the three sources,
  or ``None`` when all sources were quiet within the lookback window
  AND/OR the loader couldn't run any of them.
- ``engagement_lookback_days`` (int | None): how far back the loader
  looked. Used as the guard against false-firing on freshly-loaded
  clients with a short lookback.
- ``winning_source`` (str | None): which source's timestamp won the
  MAX; surfaced into ``signal_evidence`` so the Chat card / Gmail
  draft tells the operator which engagement type to look at.

Why deterministic, no LLM: the rule is calendar math + threshold
comparison. The PRD §6.4 spec describes owner disengagement as a
count-based signal — the LLM doesn't add value over a clean threshold.
"""

from __future__ import annotations

from ..models import ClientState, Flag, Segment, Severity, utc_now


class OwnerDisengagementSignal:
    """No qualifying engagement past the threshold = one fired flag.

    Loader-side gating (active contract + has CRM contact) means this
    signal only sees accounts where Owner Disengagement is meaningfully
    detectable; the signal short-circuits anyway if those gates fail
    so the loader can be defensive without coupling.
    """

    name = "Owner Disengagement"
    severity = Severity.CRITICAL

    def __init__(
        self,
        *,
        days_threshold: int = 14,
        segment: Segment = Segment.LOCAL_SERVICE,
        now_fn: type[utc_now] | None = None,
    ) -> None:
        self._threshold = days_threshold
        self._segment = segment
        self._now_fn = now_fn or utc_now

    def evaluate(self, client_state: ClientState) -> Flag | None:
        last_engagement = client_state.extras.get("last_engagement_at")
        lookback = client_state.extras.get("engagement_lookback_days")
        winning_source = client_state.extras.get("winning_source")

        if last_engagement is not None:
            now = self._now_fn()  # type: ignore[operator]
            delta = now - last_engagement
            days_since = max(delta.days, 0)
            if days_since < self._threshold:
                return None
            evidence = f"Last engagement was {days_since} days ago" + (
                f" via {winning_source}." if winning_source else "."
            )
        else:
            # No qualifying engagement event in the lookback window
            # across ANY source. Only fire if the lookback covered at
            # least the threshold; otherwise we'd false-fire on
            # freshly-loaded clients with a short lookback.
            if not isinstance(lookback, int) or lookback < self._threshold:
                return None
            evidence = (
                f"No engagement (calendar, triage inbound, or task "
                f"approval/completion) in the last {lookback} days."
            )

        reasoning = (
            f"{self._segment.value} risk profile flags 'Owner Disengagement' "
            "when the agency owner has had no meaningful engagement with the "
            "client past the threshold. Engagement is the MAX across three "
            "sources: calendar events with the contact, inbound triaged "
            "messages from the contact, and owner-actioned tasks (approved "
            "or marked done). Active-contract and has-CRM-contact gates run "
            "loader-side so this signal only evaluates accounts where "
            "engagement is reasonable to expect."
        )
        return Flag(
            pattern_name=self.name,
            severity=self.severity,
            account_id=client_state.account_id,
            project_id=client_state.project_id,
            segment=self._segment,
            signal_evidence=evidence,
            reasoning=reasoning,
            confidence=0.85,
            data_sources=(
                "google_calendar",
                "airtable_replica.tasks",
                "airtable_replica.contacts",
                "airtable_replica.contracts",
            ),
            baseline_snapshot=None,
        )
