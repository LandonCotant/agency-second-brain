"""REFLECT-mode dispatcher — routes a ``ReflectExtractionPayload`` to writers
and the Pub/Sub publisher (ADR 0040 §6).

The dispatcher's contract is "best effort, aggregate failures." A single
decision-write failure must NOT block subsequent wins/todos — the daily
ritual is load-bearing, partial extraction is acceptable, and the
unwritten row's outcome surfaces in audit.

Provenance threading (ADR 0040 §8): every dispatched row carries
``source_reflection_id = reflection_id`` and ``agent_run_id = run_id`` so
30/90/365-day retros can JOIN back to the reflection that birthed the
decision/win.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .extracted_writers import DecisionsWriter, WinsWriter, WriteOutcome
from .models import ReflectExtractionPayload
from .triage_publisher import PublishOutcome, ReflectTriagePublisher

log = logging.getLogger("agency_brain.agents.evening_reflection.reflect_dispatch")


@dataclass(frozen=True)
class DispatchSummary:
    """Per-tick result, surfaced in the audit row's ``output`` field.

    Counts make for cheap monitoring; the per-row ``WriteOutcome`` /
    ``PublishOutcome`` lists are kept so the audit row carries error
    details when a sub-dispatch failed.
    """

    decisions_written: int
    decisions_skipped: int
    wins_written: int
    wins_skipped: int
    todos_published: int
    todos_failed: int
    decision_outcomes: tuple[WriteOutcome, ...]
    win_outcomes: tuple[WriteOutcome, ...]
    todo_outcomes: tuple[PublishOutcome, ...]


def dispatch(
    payload: ReflectExtractionPayload,
    *,
    reflection_id: str,
    agent_run_id: str | None,
    decisions_writer: DecisionsWriter | None,
    wins_writer: WinsWriter | None,
    triage_publisher: ReflectTriagePublisher | None,
    now: datetime | None = None,
) -> DispatchSummary:
    """Route the extracted rows.

    Each writer/publisher is optional — if ``decisions_writer is None``
    the decisions array is silently dropped (e.g. unit tests don't
    always wire all three; PROMPT mode never invokes this dispatcher).
    Production REFLECT-mode tick wires all three.
    """
    decision_outcomes: list[WriteOutcome] = []
    if decisions_writer is not None:
        for ed in payload.decisions:
            outcome = decisions_writer.write(
                ed,
                reflection_id=reflection_id,
                agent_run_id=agent_run_id,
                now=now,
            )
            decision_outcomes.append(outcome)

    win_outcomes: list[WriteOutcome] = []
    if wins_writer is not None:
        for ew in payload.wins:
            outcome = wins_writer.write(
                ew,
                reflection_id=reflection_id,
                agent_run_id=agent_run_id,
                now=now,
            )
            win_outcomes.append(outcome)

    todo_outcomes: list[PublishOutcome] = []
    if triage_publisher is not None:
        for et in payload.todos:
            outcome = triage_publisher.publish(
                et,
                reflection_id=reflection_id,
                now=now,
            )
            todo_outcomes.append(outcome)

    return DispatchSummary(
        decisions_written=sum(1 for o in decision_outcomes if o.written),
        decisions_skipped=sum(1 for o in decision_outcomes if not o.written),
        wins_written=sum(1 for o in win_outcomes if o.written),
        wins_skipped=sum(1 for o in win_outcomes if not o.written),
        todos_published=sum(1 for o in todo_outcomes if o.published),
        todos_failed=sum(1 for o in todo_outcomes if not o.published),
        decision_outcomes=tuple(decision_outcomes),
        win_outcomes=tuple(win_outcomes),
        todo_outcomes=tuple(todo_outcomes),
    )


def render_dispatch_summary_block(payload: ReflectExtractionPayload) -> str:
    """Append a structured-rows summary to the prose draft body (ADR 0040 §5).

    The Gmail draft body = ``commentary`` + this block so the human
    reviewer can spot-check provenance before any retros read the
    canonical tables. Empty arrays elide their row.
    """
    parts: list[str] = []
    if payload.decisions:
        parts.append("### Decisions extracted")
        for d in payload.decisions:
            ctx = f" — {d.context.strip()}" if d.context and d.context.strip() else ""
            src = f" [from {d.source_voice_note_id}]" if d.source_voice_note_id else ""
            parts.append(f"- {d.title}{ctx}{src}")
    if payload.wins:
        parts.append("### Wins extracted")
        for w in payload.wins:
            summary = f" — {w.summary.strip()}" if w.summary and w.summary.strip() else ""
            src = f" [from {w.source_voice_note_id}]" if w.source_voice_note_id else ""
            parts.append(f"- {w.title}{summary}{src}")
    if payload.todos:
        parts.append("### Todos surfaced (published to triage)")
        for t in payload.todos:
            src = f" [from {t.source_voice_note_id}]" if t.source_voice_note_id else ""
            parts.append(f"- {t.body}{src}")
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts)


__all__ = [
    "DispatchSummary",
    "dispatch",
    "render_dispatch_summary_block",
]
