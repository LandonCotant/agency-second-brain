"""Channel-agnostic message formatters for WS-D fan-out.

Chat landed first (ADR 0023); Gmail-draft formatter joined per ADR 0032.
Both channels read from the same :class:`RoutingMessageContext` to keep
the orchestrator wiring simple — extra channel-specific fields stay
optional.
"""

from __future__ import annotations

from dataclasses import dataclass

_AIRTABLE_BASE_URL = "https://airtable.com"


@dataclass(frozen=True)
class RoutingMessageContext:
    """Inputs the channel formatters need from a `triaged_items` row.

    Mirrors the columns selected by
    `routing.polling.build_triaged_items_poll_query` so the orchestrator
    can pass through values directly without an intermediate adapter.

    Channel-specific fields (e.g. ``gmail_thread_id``) stay optional so
    a single context object serves all channels.
    """

    item_id: str
    severity: str
    source: str
    action_type: str
    reasoning: str
    source_url: str | None = None
    source_event_ref: str | None = None
    owner_email: str | None = None
    airtable_task_record_id: str | None = None
    airtable_base_id: str | None = None
    airtable_tasks_table_id: str | None = None
    # ADR 0032: present only when the upstream Gmail-to-Pub/Sub publisher
    # (WS-B PR-4, deferred) stamps thread metadata. None today; threading
    # lights up automatically once that publisher ships.
    gmail_thread_id: str | None = None
    subject: str | None = None  # original signal subject, used for Gmail subject prefix
    # ADR 0041: decisions-reviewer source carries extra fields the
    # generic formatters don't use. Populated only when source ==
    # "decisions_reviewer"; the Chat + Gmail formatters branch on that
    # source string and render the BQ console UPDATE template.
    decision_title: str | None = None
    decision_context: str | None = None
    decision_choice: str | None = None
    source_voice_note_id: str | None = None
    bq_project_id: str | None = None  # for the UPDATE template's table FQN


# Backwards-compatible alias — older imports still work for one
# transitional release. Tests + entrypoint will migrate to
# RoutingMessageContext directly.
ChatMessageContext = RoutingMessageContext


def format_chat_message(ctx: ChatMessageContext) -> str:
    """Return a single-string Chat message for one triaged item.

    Layout:
    - Severity badge + source + action type
    - Reasoning (truncated to ~280 chars)
    - Owner mention (if present)
    - Source link (if present) and Airtable Task link (if materialized)

    Kept deliberately simple text/markdown — no Card v2 — so the same
    payload works through the incoming-webhook path (ADR 0023's choice).

    For ``source == "decisions_reviewer"`` (ADR 0041), the layout
    switches to a refine-prompt: title + context preview + a hint
    pointing at the BQ console for filling alternatives + prediction +
    confidence and flipping status to ``pending``.
    """
    if ctx.source == "decisions_reviewer":
        return _format_chat_decision(ctx)

    severity_badge = _severity_badge(ctx.severity)
    header = f"{severity_badge} *{ctx.source}* — {ctx.action_type}"

    reasoning = (ctx.reasoning or "").strip()
    if len(reasoning) > 280:
        reasoning = reasoning[:277].rstrip() + "..."

    lines = [header]
    if reasoning:
        lines.append(reasoning)

    detail_bits: list[str] = []
    if ctx.owner_email:
        detail_bits.append(f"owner: {ctx.owner_email}")
    if ctx.source_url:
        detail_bits.append(f"<{ctx.source_url}|signal>")
    elif ctx.source_event_ref:
        detail_bits.append(f"ref: `{ctx.source_event_ref}`")
    task_link = _airtable_task_link(ctx)
    if task_link is not None:
        detail_bits.append(f"<{task_link}|task>")
    if detail_bits:
        lines.append(" · ".join(detail_bits))

    lines.append(f"_item_id: `{ctx.item_id}`_")
    return "\n".join(lines)


def format_gmail_draft(ctx: RoutingMessageContext) -> tuple[str, str]:
    """Return ``(subject, body_markdown)`` for a Gmail draft (ADR 0032).

    The draft is recipient-agnostic — the dispatcher decides whether
    it lands in the operator's mailbox or an owner's. Body is plain text per
    ADR 0029 §3 / ADR 0032 v1 (no HTML multipart).

    Threading: when ``ctx.gmail_thread_id`` is set, the dispatcher
    threads the draft inside the original conversation and the
    formatter prefixes the subject with ``Re:`` so the rendered
    threading view matches Gmail conventions.

    For ``source == "decisions_reviewer"`` (ADR 0041), the body
    embeds a copy-paste BQ console UPDATE template for filling
    alternatives + prediction + confidence and flipping status to
    ``pending``.
    """
    if ctx.source == "decisions_reviewer":
        return _format_gmail_decision(ctx)

    severity_label = _severity_label(ctx.severity)
    subject_seed = (ctx.subject or "").strip()
    if not subject_seed:
        subject_seed = f"{ctx.action_type} — {ctx.source}"

    if ctx.gmail_thread_id and not subject_seed.lower().startswith("re:"):
        subject_seed = f"Re: {subject_seed}"

    subject = f"[{severity_label}] {subject_seed}"
    if len(subject) > 200:
        subject = subject[:197].rstrip() + "..."

    body_lines: list[str] = []
    body_lines.append(f"Severity: {severity_label} ({ctx.source})")
    body_lines.append(f"Action: {ctx.action_type}")
    if ctx.owner_email:
        body_lines.append(f"Owner: {ctx.owner_email}")
    body_lines.append("")  # blank line before reasoning

    reasoning = (ctx.reasoning or "").strip()
    if reasoning:
        body_lines.append(reasoning)
        body_lines.append("")

    if ctx.source_url:
        body_lines.append(f"Source: {ctx.source_url}")
    elif ctx.source_event_ref:
        body_lines.append(f"Reference: {ctx.source_event_ref}")

    task_link = _airtable_task_link(ctx)
    if task_link:
        body_lines.append(f"Airtable task: {task_link}")

    body_lines.append("")
    body_lines.append(f"item_id: {ctx.item_id}")
    body_markdown = "\n".join(body_lines)
    return subject, body_markdown


# --------------------------------------------------------------- helpers


def _severity_badge(severity: str) -> str:
    badges = {
        "critical": "[CRITICAL]",
        "high": "[high]",
        "medium": "[med]",
        "low": "[low]",
        "info": "[info]",
    }
    return badges.get(severity.lower(), f"[{severity}]")


def _severity_label(severity: str) -> str:
    """Plain-text severity label for Gmail subjects (no brackets)."""
    return severity.upper() if severity else "?"


def _airtable_task_link(ctx: RoutingMessageContext) -> str | None:
    if not ctx.airtable_task_record_id:
        return None
    if not (ctx.airtable_base_id and ctx.airtable_tasks_table_id):
        return None
    return (
        f"{_AIRTABLE_BASE_URL}/{ctx.airtable_base_id}/"
        f"{ctx.airtable_tasks_table_id}/{ctx.airtable_task_record_id}"
    )


# --------------------------------------------------- decisions-reviewer (ADR 0041)


def _decision_origin_label(decision_id: str) -> str:
    """Human-readable origin from the decision_id prefix."""
    if decision_id.startswith("captures-decision-"):
        return "captures form"
    if decision_id.startswith("reflection-"):
        return "voice memo"
    return "unknown source"


def _format_chat_decision(ctx: RoutingMessageContext) -> str:
    """Chat layout for a draft decision (ADR 0041).

    Branches on ``source == "decisions_reviewer"`` from
    :func:`format_chat_message`. The card surfaces the title + a
    truncated context preview and points at the BQ console for the
    refine UPDATE; the Gmail draft body carries the actual paste-ready
    template.
    """
    title = (ctx.decision_title or "(untitled decision)").strip()
    context_preview = (ctx.decision_context or "").strip()
    if len(context_preview) > 280:
        context_preview = context_preview[:277].rstrip() + "..."

    origin = _decision_origin_label(ctx.item_id)

    lines: list[str] = [
        f"*Draft decision to refine* — {origin}",
        f"*{title}*",
    ]
    if context_preview:
        lines.append(context_preview)
    lines.append(
        "Refine in BQ console: set alternatives + prediction + confidence on "
        "`agent_outputs.decisions`, flip status to 'pending'. (See Gmail draft "
        "for paste-ready UPDATE.)"
    )
    lines.append(f"_decision_id: `{ctx.item_id}`_")
    return "\n".join(lines)


def _format_gmail_decision(ctx: RoutingMessageContext) -> tuple[str, str]:
    """Gmail draft body for a draft decision (ADR 0041).

    Subject is the row-converter-set ``ctx.subject`` (already prefixed
    with ``[DECISION DRAFT]``). Body embeds a copy-paste BQ console
    UPDATE so refining is mechanical: open draft → fill blanks → paste
    in BQ console → run.
    """
    title = (ctx.decision_title or "(untitled decision)").strip()
    subject_seed = (ctx.subject or f"[DECISION DRAFT] {title}").strip()
    if len(subject_seed) > 200:
        subject_seed = subject_seed[:197].rstrip() + "..."

    origin = _decision_origin_label(ctx.item_id)
    table_fqn = (
        f"`{ctx.bq_project_id}.agent_outputs.decisions`"
        if ctx.bq_project_id
        else "`agent_outputs.decisions`"
    )

    body_lines: list[str] = [
        f"Draft decision: {title}",
        "",
    ]
    context_text = (ctx.decision_context or "").strip()
    if context_text:
        body_lines.append("Context:")
        body_lines.append(context_text)
        body_lines.append("")
    choice_text = (ctx.decision_choice or "").strip()
    if choice_text:
        body_lines.append(f"Current choice: {choice_text}")
        body_lines.append("")

    body_lines.append(f"Source: {origin} ({ctx.item_id})")
    if ctx.source_voice_note_id:
        body_lines.append(f"Voice memo note_id: {ctx.source_voice_note_id}")
    body_lines.append("")

    body_lines.append(
        "To refine, fill in the blanks and run in BQ console "
        "(once a row dispatches, routed_events dedup keeps it from re-surfacing):"
    )
    body_lines.append("")
    body_lines.append(f"UPDATE {table_fqn}")
    body_lines.append("SET")
    body_lines.append("  alternatives = ['<alt 1>', '<alt 2>'],")
    body_lines.append("  prediction = '<testable 90-day prediction>',")
    body_lines.append("  confidence = <0.0 to 1.0>,")
    body_lines.append("  refined_at = CURRENT_TIMESTAMP(),")
    body_lines.append("  status = 'pending'")
    body_lines.append(f"WHERE decision_id = '{ctx.item_id}';")

    return subject_seed, "\n".join(body_lines)
