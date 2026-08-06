"""Morning Brief composer — renders prompt + LLM call + markdown output.

Per ADR 0029: Vertex SDK direct (`gemini-2.5-flash`), NOT a Reasoning
Engine. Avoids the orphan-RE cost incident posture (PR #56 / ADR 0028).

The composer's input is the full set of section blocks already rendered
by `agent.py` (which calls the readers + calendar client). The composer
substitutes them into `prompts/morning_brief/v1.md` and calls the LLM.
The output is a markdown body string.

`LLMComposeClient` Protocol keeps unit tests fast — they pass a stub that
returns a canned markdown body without touching Vertex.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

log = logging.getLogger("agency_brain.agents.morning_brief.composer")

DEFAULT_MODEL = "gemini-2.5-flash"


class LLMComposeClient(Protocol):
    """Minimal LLM surface — `generate(prompt) -> markdown_string`."""

    def compose(self, *, prompt: str, model: str) -> str: ...


class MorningBriefComposer:
    """Renders the prompt + invokes the LLM."""

    def __init__(
        self,
        *,
        prompt_template: str,
        llm: LLMComposeClient,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._template = prompt_template
        self._llm = llm
        self._model = model

    def compose(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        run_date: date,
        triaged_items_block: str,
        open_tasks_block: str,
        risk_flags_block: str,
        drafts_awaiting_block: str,
        calendar_block: str,
    ) -> str:
        prompt = self._template
        substitutions = {
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "run_date_human": run_date.strftime("%A, %B %d, %Y"),
            "triaged_items_block": triaged_items_block,
            "open_tasks_block": open_tasks_block,
            "risk_flags_block": risk_flags_block,
            "drafts_awaiting_block": drafts_awaiting_block,
            "calendar_block": calendar_block,
        }
        for key, value in substitutions.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
        body = self._llm.compose(prompt=prompt, model=self._model).strip()
        return body or _quiet_day_fallback(run_date)


def _quiet_day_fallback(run_date: date) -> str:
    """If the LLM returns empty (rare), don't break the daily ritual."""
    return f"Quiet day — {run_date.strftime('%A, %B %d')}: nothing on the brief."


def render_section_blocks(
    *,
    triaged_items: list,
    open_tasks: list,
    risk_flags: list,
    drafts_awaiting: list,
    calendar_events: list,
    timezone: str = "America/Los_Angeles",
) -> dict[str, str]:
    """Render each input list into a textual block for the prompt.

    Empty blocks are rendered as the literal text "(none)" so the LLM
    can apply its "omit the section if empty" rule cleanly.
    """
    return {
        "triaged_items_block": _render_triaged_items(triaged_items),
        "open_tasks_block": _render_open_tasks(open_tasks),
        "risk_flags_block": _render_risk_flags(risk_flags),
        "drafts_awaiting_block": _render_drafts(drafts_awaiting),
        "calendar_block": _render_calendar(calendar_events, timezone),
    }


def _render_triaged_items(items: list) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        url_suffix = f" — {it.source_url}" if getattr(it, "source_url", None) else ""
        lines.append(f"- [{it.severity}] {it.summary} (source={it.source}){url_suffix}")
    return "\n".join(lines)


def _render_open_tasks(tasks: list) -> str:
    if not tasks:
        return "(none)"
    lines = []
    for t in tasks:
        due = t.due_date.isoformat() if getattr(t, "due_date", None) else "no due date"
        proj = f" / {t.project_name}" if getattr(t, "project_name", None) else ""
        lines.append(f"- {t.name} (due {due}){proj}")
    return "\n".join(lines)


def _render_risk_flags(flags: list) -> str:
    if not flags:
        return "(none)"
    lines = []
    for f in flags:
        acct = f" on {f.account_name}" if getattr(f, "account_name", None) else ""
        reason = f.reasoning or ""
        lines.append(f"- [{f.severity}] {f.pattern_name}{acct}: {reason}")
    return "\n".join(lines)


def _render_drafts(drafts: list) -> str:
    if not drafts:
        return "(none)"
    lines = []
    for d in drafts:
        proj = f" / {d.project_name}" if getattr(d, "project_name", None) else ""
        lines.append(f"- {d.name}{proj}")
    return "\n".join(lines)


def _render_calendar(events: list, timezone: str) -> str:
    if not events:
        return "(none)"
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    lines = []
    for e in events:
        start_local = e.start.astimezone(tz).strftime("%H:%M")
        end_local = e.end.astimezone(tz).strftime("%H:%M")
        attendees_n = len(e.attendees) if hasattr(e, "attendees") else 0
        attendees_suffix = f" ({attendees_n} attendees)" if attendees_n > 4 else ""
        lines.append(f"- {start_local}–{end_local}: {e.summary}{attendees_suffix}")
    return "\n".join(lines)
