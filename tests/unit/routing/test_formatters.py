"""Unit tests for routing.formatters (Chat + Gmail message layout, ADR 0023, 0032)."""

from __future__ import annotations

from agency_brain.routing.formatters import (
    RoutingMessageContext,
    format_chat_message,
    format_gmail_draft,
)


def _ctx(**overrides) -> RoutingMessageContext:
    base = dict(
        item_id="item-123",
        severity="critical",
        source="gmail",
        action_type="reply_today",
        reasoning="Client asked when the H2 retainer renews. They expect a same-day response.",
        source_url="https://mail.google.com/mail/u/0/#inbox/abc123",
        source_event_ref="msg-abc123",
        owner_email="owner@example.com",
    )
    base.update(overrides)
    return RoutingMessageContext(**base)


def test_critical_message_includes_badge_source_action() -> None:
    msg = format_chat_message(_ctx())
    assert "[CRITICAL]" in msg
    assert "*gmail*" in msg
    assert "reply_today" in msg


def test_high_message_uses_lowercase_badge() -> None:
    msg = format_chat_message(_ctx(severity="high"))
    assert "[high]" in msg


def test_unknown_severity_is_passed_through_in_brackets() -> None:
    msg = format_chat_message(_ctx(severity="urgent"))
    assert "[urgent]" in msg


def test_long_reasoning_is_truncated_with_ellipsis() -> None:
    long_reason = "x" * 500
    msg = format_chat_message(_ctx(reasoning=long_reason))
    # Truncated to 277 chars + "..." = max 280 chars worth of reasoning
    reasoning_line = next(line for line in msg.split("\n") if line.startswith("x"))
    assert len(reasoning_line) <= 280
    assert reasoning_line.endswith("...")


def test_owner_email_appears_in_detail_line() -> None:
    msg = format_chat_message(_ctx())
    assert "owner: owner@example.com" in msg


def test_signal_link_uses_source_url_when_present() -> None:
    msg = format_chat_message(_ctx())
    assert "<https://mail.google.com/mail/u/0/#inbox/abc123|signal>" in msg


def test_event_ref_used_when_no_source_url() -> None:
    msg = format_chat_message(_ctx(source_url=None))
    assert "ref: `msg-abc123`" in msg
    assert "|signal>" not in msg


def test_airtable_task_link_appears_when_record_id_and_base_provided() -> None:
    msg = format_chat_message(
        _ctx(
            airtable_task_record_id="recXYZ123",
            airtable_base_id="appXXXXXXXXXXXXXX",
            airtable_tasks_table_id="tblTASKS",
        )
    )
    assert "<https://airtable.com/appXXXXXXXXXXXXXX/tblTASKS/recXYZ123|task>" in msg


def test_airtable_link_omitted_when_base_or_table_missing() -> None:
    msg = format_chat_message(
        _ctx(airtable_task_record_id="recXYZ123")  # no base/table id
    )
    assert "airtable.com" not in msg
    assert "|task>" not in msg


def test_item_id_appears_in_message() -> None:
    msg = format_chat_message(_ctx())
    assert "item-123" in msg


def test_empty_reasoning_does_not_emit_blank_line() -> None:
    msg = format_chat_message(_ctx(reasoning=""))
    assert "\n\n" not in msg


# --------------------------------------------------------------- Gmail formatter


def test_gmail_subject_includes_severity_and_seed() -> None:
    subject, _ = format_gmail_draft(_ctx(subject="Q3 retainer question"))
    assert subject.startswith("[CRITICAL]")
    assert "Q3 retainer question" in subject


def test_gmail_subject_falls_back_to_action_when_no_subject() -> None:
    subject, _ = format_gmail_draft(_ctx())
    assert "[CRITICAL]" in subject
    assert "reply_today" in subject
    assert "gmail" in subject


def test_gmail_subject_capped_at_200_chars() -> None:
    long_subject = "x" * 500
    subject, _ = format_gmail_draft(_ctx(subject=long_subject))
    assert len(subject) <= 200


def test_gmail_subject_prefixed_re_when_threaded() -> None:
    subject, _ = format_gmail_draft(_ctx(subject="Q3 retainer", gmail_thread_id="thread-1"))
    assert "Re:" in subject


def test_gmail_subject_no_double_re_when_already_re() -> None:
    subject, _ = format_gmail_draft(_ctx(subject="Re: Q3 retainer", gmail_thread_id="thread-1"))
    assert subject.count("Re:") == 1


def test_gmail_body_includes_severity_action_reasoning_and_item_id() -> None:
    _, body = format_gmail_draft(_ctx())
    assert "Severity: CRITICAL" in body
    assert "Action: reply_today" in body
    assert "Client asked when the H2 retainer renews" in body
    assert "item_id: item-123" in body


def test_gmail_body_includes_owner_when_present() -> None:
    _, body = format_gmail_draft(_ctx())
    assert "Owner: owner@example.com" in body


def test_gmail_body_includes_source_link_when_present() -> None:
    _, body = format_gmail_draft(_ctx())
    assert "https://mail.google.com/mail/u/0/#inbox/abc123" in body


def test_gmail_body_falls_back_to_event_ref_when_no_source_url() -> None:
    _, body = format_gmail_draft(_ctx(source_url=None))
    assert "Reference: msg-abc123" in body


def test_gmail_body_includes_airtable_task_link() -> None:
    _, body = format_gmail_draft(
        _ctx(
            airtable_task_record_id="recXYZ123",
            airtable_base_id="appXXXXXXXXXXXXXX",
            airtable_tasks_table_id="tblTASKS",
        )
    )
    assert "https://airtable.com/appXXXXXXXXXXXXXX/tblTASKS/recXYZ123" in body


# --------------------------------------------------- decisions-reviewer (ADR 0041)


def _decision_ctx(**overrides) -> RoutingMessageContext:
    base = dict(
        item_id="captures-decision-recABC",
        severity="high",
        source="decisions_reviewer",
        action_type="refine_decision",
        reasoning="Three users complained the brief feels stale by 8am.",
        subject="[DECISION DRAFT] Migrate the brief composer to streaming",
        decision_title="Migrate the brief composer to streaming",
        decision_context="Three users complained the brief feels stale by 8am.",
        decision_choice="Switch to streaming generation behind a feature flag.",
        bq_project_id="agency-brain-demo",
    )
    base.update(overrides)
    return RoutingMessageContext(**base)


def test_chat_decision_card_shows_origin_label_for_captures() -> None:
    msg = format_chat_message(_decision_ctx())
    assert "Draft decision to refine" in msg
    assert "captures form" in msg


def test_chat_decision_card_shows_origin_label_for_voice_memo() -> None:
    msg = format_chat_message(_decision_ctx(item_id="reflection-drive-file-abc-1234567890ab"))
    assert "voice memo" in msg


def test_chat_decision_card_includes_title_and_context_preview() -> None:
    msg = format_chat_message(_decision_ctx())
    assert "Migrate the brief composer to streaming" in msg
    assert "Three users complained" in msg


def test_chat_decision_card_truncates_long_context() -> None:
    long_context = "x" * 500
    msg = format_chat_message(_decision_ctx(decision_context=long_context))
    truncated = next(line for line in msg.split("\n") if line.startswith("x"))
    assert len(truncated) <= 280
    assert truncated.endswith("...")


def test_chat_decision_card_points_at_gmail_for_update() -> None:
    """Card is a notification, not the editing UI — the UPDATE template lives in Gmail."""
    msg = format_chat_message(_decision_ctx())
    assert "Gmail draft" in msg
    assert "agent_outputs.decisions" in msg


def test_chat_decision_card_includes_decision_id() -> None:
    msg = format_chat_message(_decision_ctx())
    assert "captures-decision-recABC" in msg


def test_chat_decision_card_handles_missing_title_gracefully() -> None:
    msg = format_chat_message(_decision_ctx(decision_title=None))
    assert "untitled decision" in msg


def test_gmail_decision_subject_uses_row_converter_seed() -> None:
    """Row converter sets [DECISION DRAFT] prefix; formatter passes through."""
    subject, _ = format_gmail_draft(_decision_ctx())
    assert subject == "[DECISION DRAFT] Migrate the brief composer to streaming"


def test_gmail_decision_subject_capped_at_200_chars() -> None:
    long_subject = "[DECISION DRAFT] " + "x" * 500
    subject, _ = format_gmail_draft(_decision_ctx(subject=long_subject))
    assert len(subject) <= 200


def test_gmail_decision_body_includes_paste_ready_update_template() -> None:
    _, body = format_gmail_draft(_decision_ctx())
    assert "UPDATE `agency-brain-demo.agent_outputs.decisions`" in body
    assert "alternatives = ['<alt 1>', '<alt 2>']" in body
    assert "prediction = '<testable 90-day prediction>'" in body
    assert "confidence = <0.0 to 1.0>" in body
    assert "status = 'pending'" in body
    assert "refined_at = CURRENT_TIMESTAMP()" in body
    assert "WHERE decision_id = 'captures-decision-recABC';" in body


def test_gmail_decision_body_includes_origin_label_and_id() -> None:
    _, body = format_gmail_draft(_decision_ctx())
    assert "Source: captures form (captures-decision-recABC)" in body


def test_gmail_decision_body_surfaces_voice_note_id_when_present() -> None:
    _, body = format_gmail_draft(
        _decision_ctx(
            item_id="reflection-drive-file-abc-1234567890ab",
            source_voice_note_id="drive-file-abc-rev-1",
        )
    )
    assert "Voice memo note_id: drive-file-abc-rev-1" in body
    assert "Source: voice memo" in body


def test_gmail_decision_body_omits_voice_note_line_when_absent() -> None:
    _, body = format_gmail_draft(_decision_ctx())
    assert "Voice memo note_id" not in body


def test_gmail_decision_body_includes_title_context_choice() -> None:
    _, body = format_gmail_draft(_decision_ctx())
    assert "Draft decision: Migrate the brief composer to streaming" in body
    assert "Three users complained" in body
    assert "Current choice: Switch to streaming generation" in body


def test_gmail_decision_body_falls_back_to_unqualified_table_when_no_project() -> None:
    """When bq_project_id is None, the FQN is best-effort — better than crashing."""
    _, body = format_gmail_draft(_decision_ctx(bq_project_id=None))
    assert "UPDATE `agent_outputs.decisions`" in body
