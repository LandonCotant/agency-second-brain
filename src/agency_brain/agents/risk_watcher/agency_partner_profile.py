"""Agency Partner risk profile (ADR 0034 §5 — skeleton).

Per ADR 0034 §5, all five spec'd Agency Partner signals are blocked
on Vantage federation tables that aren't yet provisioned. PR-E ships
the segment wiring with an empty signal tuple so the multi-segment
loop in `main.py` is structurally complete; the post-Vantage PR fills
in the signal classes by extending this factory.

The thresholds dict is accepted (and ignored) so the call shape is
symmetric with the other two profile factories — `main.py` doesn't
have to special-case AP today or tomorrow.
"""

from __future__ import annotations

from typing import Any

from .models import Profile, Segment


def build_agency_partner_profile(
    thresholds: dict[str, dict[str, Any]] | None = None,
) -> Profile:
    """Empty profile. The skip-if-empty-signals branch in the segment
    loop short-circuits this profile's tick until Vantage lands.
    """
    del thresholds  # unused until Vantage signals are wired
    return Profile(segment=Segment.AGENCY_PARTNER, signals=())


# Default profile (PR-E placeholder). Production builds the profile
# via ``build_agency_partner_profile(thresholds)`` at startup inside
# the per-segment loop in `main.py`.
AGENCY_PARTNER_PROFILE: Profile = build_agency_partner_profile()
