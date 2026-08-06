"""Unit tests for the Cloud Run Job entrypoint glue (ADR 0023, 0032)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agency_brain.routing.fanout_main import (
    _DryRunBQ,
    _DryRunChatClient,
    _DryRunGmailClient,
    decision_row_to_input,
    make_decision_row_to_input,
    risk_flag_row_to_input,
    row_to_input,
)


def _row(severity: str = "critical") -> dict[str, Any]:
    return {
        "item_id": f"item-{severity}",
        "triaged_at": datetime(2026, 4, 30, 18, 0, tzinfo=UTC),
        "source": "gmail",
        "source_url": "https://example/x",
        "source_event_ref": "ref-1",
        "actionable": True,
        "owner_type": "delegate",
        "owner_email": "owner@example.com",
        "action_type": "reply_today",
        "severity": severity,
        "confidence": 0.9,
        "human_review_routed": False,
        "reasoning": "client said website is down",
        "airtable_task_record_id": None,
    }


def test_row_to_input_maps_required_fields() -> None:
    inp = row_to_input(_row())

    assert inp.item.item_id == "item-critical"
    assert inp.item.severity == "critical"
    assert inp.item.owner_email == "owner@example.com"
    assert inp.message_context.source == "gmail"
    assert inp.message_context.action_type == "reply_today"
    assert inp.message_context.reasoning == "client said website is down"
    assert inp.aspects == []
    assert inp.already_routed == frozenset()


def test_row_to_input_passes_already_routed_from_polling_array() -> None:
    row = _row()
    row["routed_channels"] = ["google_chat_dm"]
    inp = row_to_input(row)
    assert inp.already_routed == frozenset({"google_chat_dm"})


def test_row_to_input_handles_null_reasoning() -> None:
    row = _row()
    row["reasoning"] = None
    inp = row_to_input(row)
    assert inp.message_context.reasoning == ""


def test_dry_run_gmail_client_does_not_call_api() -> None:
    client = _DryRunGmailClient()
    result = client.draft(
        recipient_email="owner@example.com",
        subject="[CRITICAL] reply_today — gmail",
        body_markdown="severity body",
    )
    assert result.ok is True
    assert result.draft_id == "dry-run"


def test_dry_run_chat_client_does_not_post() -> None:
    client = _DryRunChatClient()
    result = client.send("hello")
    assert result.ok is True
    assert result.status == 200


class _FakeBQ:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.routings: list[tuple[str, str, int | None]] = []

    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return [_row()]

    def record_routing(
        self,
        *,
        item_id: str,
        channel: str,
        chat_status: int | None = None,
        agent_run_id: str | None = None,
    ) -> int:
        self.routings.append((item_id, channel, chat_status))
        return 1


def test_dry_run_bq_passes_through_query_and_skips_routing_insert() -> None:
    inner = _FakeBQ()
    bq = _DryRunBQ(inner)

    rows = bq.query_rows("SELECT 1")
    assert len(rows) == 1
    assert inner.queries == ["SELECT 1"]

    affected = bq.record_routing(item_id="item-x", channel="google_chat_dm", chat_status=200)
    assert affected == 1
    # The inner BQ never received the insert — that's the point of dry-run.
    assert inner.routings == []


# ---------------------------------------------------------------------------
# risk_flag_row_to_input (ADR 0033 PR-C)
# ---------------------------------------------------------------------------


def _risk_flag_row(
    severity: str = "high",
    *,
    account_name: str | None = "Acme E-comm",
    pattern_name: str = "Acknowledgment Gap",
) -> dict[str, Any]:
    return {
        "flag_id": f"flag-{severity}-1",
        "flagged_at": datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
        "account_id": "recAcct1",
        "project_id": "recProj1",
        "segment": "E-commerce",
        "pattern_name": pattern_name,
        "severity": severity,
        "signal_evidence": "3 drafted task(s) overdue beyond 5 business days; oldest is 7 business days.",
        "reasoning": "E-commerce risk profile flags Acknowledgment Gap when a Triage-drafted Task remains unactioned.",
        "confidence": 0.95,
        "human_review_routed": False,
        "airtable_task_record_id": None,
        "account_name": account_name,
    }


def test_risk_flag_row_to_input_maps_to_fanout_input() -> None:
    inp = risk_flag_row_to_input(_risk_flag_row())

    # flag_id flows into both TriagedItem.item_id and message_context.item_id;
    # routed_events.item_id receives the flag_id at dispatch time.
    assert inp.item.item_id == "flag-high-1"
    assert inp.message_context.item_id == "flag-high-1"
    assert inp.item.severity == "high"
    assert inp.item.human_review_routed is False
    # owner is leadership-only in v1 — no owner email lookup yet.
    assert inp.item.owner_type == "leadership"
    assert inp.item.owner_email is None


def test_risk_flag_row_to_input_combines_evidence_and_reasoning() -> None:
    inp = risk_flag_row_to_input(_risk_flag_row())
    reasoning = inp.message_context.reasoning
    assert "drafted task(s) overdue" in reasoning
    assert "E-commerce risk profile flags" in reasoning
    # Visually separated by a blank line so Chat + Gmail formatting
    # render evidence and reasoning as distinct sections.
    assert "\n\n" in reasoning


def test_risk_flag_row_to_input_subject_includes_account_name() -> None:
    inp = risk_flag_row_to_input(_risk_flag_row())
    assert inp.message_context.subject == "Acknowledgment Gap — Acme E-comm"


def test_risk_flag_row_to_input_subject_falls_back_when_account_name_missing() -> None:
    """LEFT JOIN to airtable_replica.accounts can return NULL — graceful."""
    row = _risk_flag_row(account_name=None)
    inp = risk_flag_row_to_input(row)
    assert inp.message_context.subject == "Acknowledgment Gap"


def test_risk_flag_row_to_input_source_is_risk_watcher_to_block_threading() -> None:
    """Gmail adapter's threading guard keys on `source == "gmail"`."""
    inp = risk_flag_row_to_input(_risk_flag_row())
    assert inp.message_context.source == "risk_watcher"
    assert inp.message_context.gmail_thread_id is None


def test_risk_flag_row_to_input_carries_account_id_in_event_ref() -> None:
    inp = risk_flag_row_to_input(_risk_flag_row())
    assert inp.message_context.source_event_ref == "account=recAcct1"


def test_risk_flag_row_to_input_passes_already_routed_from_polling_array() -> None:
    row = _risk_flag_row()
    row["routed_channels"] = ["gmail_draft"]
    inp = risk_flag_row_to_input(row)
    assert inp.already_routed == frozenset({"gmail_draft"})


def test_risk_flag_row_to_input_handles_empty_evidence_and_reasoning() -> None:
    row = _risk_flag_row()
    row["signal_evidence"] = None
    row["reasoning"] = None
    inp = risk_flag_row_to_input(row)
    assert inp.message_context.reasoning == "(no reasoning)"


def test_risk_flag_row_to_input_action_type_is_pattern_name() -> None:
    """The Chat header `*source* — action_type` line shows the pattern."""
    inp = risk_flag_row_to_input(_risk_flag_row(pattern_name="Silent After Deliverable"))
    assert inp.message_context.action_type == "Silent After Deliverable"


# ---------------------------------------------------------------------------
# decision_row_to_input (ADR 0041)
# ---------------------------------------------------------------------------


def _decision_row(
    *,
    decision_id: str = "captures-decision-recABC123",
    title: str = "Migrate the brief composer to streaming",
    context: str | None = "Three users complained the morning brief feels stale by 8am.",
    choice: str | None = "Switch to streaming generation behind a feature flag.",
    source_voice_note_id: str | None = None,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "decided_at": datetime(2026, 5, 6, 4, 0, tzinfo=UTC),
        "title": title,
        "context": context,
        "choice": choice,
        "status": "draft",
        "source_reflection_id": None,
        "source_voice_note_id": source_voice_note_id,
    }


def test_decision_row_to_input_decision_id_flows_to_item_id() -> None:
    inp = decision_row_to_input(_decision_row(), project_id="agency-brain-demo")
    assert inp.item.item_id == "captures-decision-recABC123"
    assert inp.message_context.item_id == "captures-decision-recABC123"


def test_decision_row_to_input_synthesizes_high_severity_for_chat_window() -> None:
    """ADR 0041: severity='high' → existing Chat 09:00–16:00 PT window applies."""
    inp = decision_row_to_input(_decision_row(), project_id="p")
    assert inp.item.severity == "high"
    assert inp.message_context.severity == "high"


def test_decision_row_to_input_owner_is_leadership_with_no_owner_email() -> None:
    inp = decision_row_to_input(_decision_row(), project_id="p")
    assert inp.item.owner_type == "leadership"
    assert inp.item.owner_email is None
    assert inp.item.human_review_routed is False


def test_decision_row_to_input_source_blocks_gmail_threading() -> None:
    """source='decisions_reviewer' keeps the Gmail adapter's threading guard happy."""
    inp = decision_row_to_input(_decision_row(), project_id="p")
    assert inp.message_context.source == "decisions_reviewer"
    assert inp.message_context.action_type == "refine_decision"
    assert inp.message_context.gmail_thread_id is None


def test_decision_row_to_input_subject_prefix() -> None:
    inp = decision_row_to_input(_decision_row(), project_id="p")
    assert inp.message_context.subject == (
        "[DECISION DRAFT] Migrate the brief composer to streaming"
    )


def test_decision_row_to_input_subject_handles_empty_title() -> None:
    inp = decision_row_to_input(_decision_row(title=""), project_id="p")
    assert inp.message_context.subject == "[DECISION DRAFT]"


def test_decision_row_to_input_passes_decision_fields_to_formatter_context() -> None:
    inp = decision_row_to_input(
        _decision_row(source_voice_note_id="drive-file-abc-rev-1"),
        project_id="p",
    )
    ctx = inp.message_context
    assert ctx.decision_title == "Migrate the brief composer to streaming"
    assert ctx.decision_context is not None
    assert "Three users complained" in ctx.decision_context
    assert ctx.decision_choice is not None
    assert "streaming generation" in ctx.decision_choice
    assert ctx.source_voice_note_id == "drive-file-abc-rev-1"
    assert ctx.bq_project_id == "p"


def test_decision_row_to_input_reasoning_falls_back_to_choice_when_context_missing() -> None:
    inp = decision_row_to_input(_decision_row(context=None, choice="Just do X."), project_id="p")
    assert inp.message_context.reasoning == "Just do X."


def test_decision_row_to_input_reasoning_default_when_both_missing() -> None:
    inp = decision_row_to_input(_decision_row(context=None, choice=None), project_id="p")
    assert inp.message_context.reasoning == "(no context)"


def test_decision_row_to_input_passes_already_routed_from_polling_array() -> None:
    row = _decision_row()
    row["routed_channels"] = ["gmail_draft"]
    inp = decision_row_to_input(row, project_id="p")
    assert inp.already_routed == frozenset({"gmail_draft"})


def test_make_decision_row_to_input_closes_over_project_id() -> None:
    """The wrapper produces a one-arg row converter for run_fanout_tick."""
    convert = make_decision_row_to_input(project_id="agency-brain-demo")
    inp = convert(_decision_row())
    assert inp.message_context.bq_project_id == "agency-brain-demo"
    assert inp.item.item_id == "captures-decision-recABC123"
