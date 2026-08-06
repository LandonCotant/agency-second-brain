"""Unit tests for daily_spend_check pure-logic functions.

Per ADR 0030 the script queries billing-export BQ for the previous 24h spend,
evaluates per-project thresholds, formats a Chat card, and posts. These tests
pin the threshold logic + Chat payload shape; the BQ + webhook I/O paths are
exercised by the daily Cloud Run Job in prod.
"""

from __future__ import annotations

from agency_brain.audit.daily_spend_check import (
    evaluate_thresholds,
    format_chat_message,
)

# ---------------------------------------------------------------------------
# evaluate_thresholds
# ---------------------------------------------------------------------------


def test_evaluate_thresholds_no_breach():
    spend = {"agency-brain-demo": {"total": 1.85, "services": {"Cloud Run": 1.85}}}
    thresholds = {"agency-brain-demo": 50.0}
    assert evaluate_thresholds(spend, thresholds) == []


def test_evaluate_thresholds_returns_breach_with_top_services():
    spend = {
        "agency-brain-demo": {
            "total": 62.50,
            "services": {
                "Cloud Run": 30.0,
                "Vertex AI": 20.0,
                "BigQuery": 12.5,
            },
        }
    }
    thresholds = {"agency-brain-demo": 50.0}
    breaches = evaluate_thresholds(spend, thresholds)
    assert len(breaches) == 1
    breach = breaches[0]
    assert breach["project_id"] == "agency-brain-demo"
    assert breach["total_usd"] == 62.50
    assert breach["threshold_usd"] == 50.0
    # Top services sorted descending by cost
    assert breach["top_services"][0] == ("Cloud Run", 30.0)
    assert breach["top_services"][1] == ("Vertex AI", 20.0)


def test_evaluate_thresholds_ignores_projects_absent_from_spend():
    """Project listed in thresholds but missing from spend means 'no data
    yet today' — silent, not a breach. Matches the 'billing_export_empty'
    handling in main()."""
    spend: dict[str, dict] = {}
    thresholds = {"agency-brain-demo": 50.0}
    assert evaluate_thresholds(spend, thresholds) == []


def test_evaluate_thresholds_only_thresholded_projects_breach():
    """Projects with spend but no threshold are visibility-only — never
    flagged. Matches the env-var design: COST_THRESHOLDS_USD defines what
    is monitored; everything else is just printed."""
    spend = {
        "agency-brain-demo": {"total": 100.0, "services": {"Cloud Run": 100.0}},
        "untracked-project": {"total": 999.0, "services": {"BigQuery": 999.0}},
    }
    thresholds = {"agency-brain-demo": 50.0}
    breaches = evaluate_thresholds(spend, thresholds)
    assert [b["project_id"] for b in breaches] == ["agency-brain-demo"]


def test_evaluate_thresholds_exactly_at_threshold_does_not_breach():
    """Strict greater-than: spend == threshold is OK. Avoids alarm-fatigue
    when daily spend tracks the budget line exactly."""
    spend = {"agency-brain-demo": {"total": 50.0, "services": {"Cloud Run": 50.0}}}
    thresholds = {"agency-brain-demo": 50.0}
    assert evaluate_thresholds(spend, thresholds) == []


# ---------------------------------------------------------------------------
# format_chat_message
# ---------------------------------------------------------------------------


def _extract_text(payload: dict) -> str:
    return payload["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]


def test_format_chat_message_no_breach_no_spend():
    """Empty billing-export window: a benign 'no data' line, not a breach."""
    payload = format_chat_message(
        spend={},
        thresholds={"agency-brain-demo": 50.0},
        breaches=[],
        window_label="2026-05-28 09:00 → 2026-05-29 09:00 UTC",
    )
    text = _extract_text(payload)
    assert "No billing data for this window" in text
    assert "agency-brain-demo" not in text  # no spend → no per-project line


def test_format_chat_message_breach_header_renders():
    breach = {
        "project_id": "agency-brain-demo",
        "total_usd": 62.50,
        "threshold_usd": 50.0,
        "top_services": [("Cloud Run", 30.0)],
    }
    spend = {"agency-brain-demo": {"total": 62.50, "services": {"Cloud Run": 30.0}}}
    payload = format_chat_message(
        spend=spend,
        thresholds={"agency-brain-demo": 50.0},
        breaches=[breach],
        window_label="window",
    )
    text = _extract_text(payload)
    assert "1 project(s) over threshold" in text
    assert "$62.50" in text
    assert "threshold $50.00" in text


def test_format_chat_message_threshold_tag_inline():
    """Projects with a threshold get the inline `(≤ $N target)` annotation;
    untracked projects get a bare amount. Matches the 30-second glance brief."""
    spend = {
        "agency-brain-demo": {"total": 1.85, "services": {"Cloud Run": 1.85}},
        "secondary-project": {"total": 0.42, "services": {"BigQuery": 0.42}},
    }
    payload = format_chat_message(
        spend=spend,
        thresholds={"agency-brain-demo": 50.0},
        breaches=[],
        window_label="window",
    )
    text = _extract_text(payload)
    assert "agency-brain-demo" in text and "(≤ $50 target)" in text
    assert "secondary-project" in text and "(≤ $0 target)" not in text  # no threshold


def test_format_chat_message_sorts_projects_by_spend_descending():
    spend = {
        "low": {"total": 1.0, "services": {}},
        "high": {"total": 100.0, "services": {}},
        "mid": {"total": 10.0, "services": {}},
    }
    payload = format_chat_message(
        spend=spend,
        thresholds={},
        breaches=[],
        window_label="w",
    )
    text = _extract_text(payload)
    assert text.index("high") < text.index("mid") < text.index("low")


def test_format_chat_message_card_id_stable():
    """`asb-cost-daily-check` is the cardId Chat uses to dedup updates — pin it
    so a future rename doesn't accidentally break Chat threading."""
    payload = format_chat_message(spend={}, thresholds={}, breaches=[], window_label="w")
    assert payload["cardsV2"][0]["cardId"] == "asb-cost-daily-check"
