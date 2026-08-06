"""Evening Reflection composer — renders prompt + LLM call + markdown output.

Per ADR 0036 §2: Vertex SDK direct (``gemini-2.5-flash``), NOT a Reasoning
Engine. Same posture as ADR 0029 §3 — avoids the orphan-RE cost incident
pattern from 2026-05-02 (ADR 0028).

The composer's input is the full set of section blocks already rendered
by ``agent.py`` (which calls the readers + calendar client). The composer
substitutes them into ``prompts/evening_reflection/v1.md`` and calls the
LLM. The output is a markdown body string (three prose sections, or a
single-paragraph "unremarkable day" body when the LLM returns empty).

``LLMComposeClient`` Protocol keeps unit tests fast — they pass a stub
that returns a canned markdown body without touching Vertex.
"""

from __future__ import annotations

import html
import logging
from datetime import date
from typing import Protocol

from ...common.standard_questions import StandardQuestions
from .areas_context_reader import AreaNoteSnippet
from .models import ReflectExtractionPayload
from .structured_compose import StructuredLLMClient, parse_extraction_payload

log = logging.getLogger("agency_brain.agents.evening_reflection.composer")

DEFAULT_MODEL = "gemini-2.5-flash"


class LLMComposeClient(Protocol):
    """Minimal LLM surface — ``compose(prompt, model) -> markdown_string``."""

    def compose(self, *, prompt: str, model: str) -> str: ...


class EveningReflectionComposer:
    """Renders the prompt + invokes the LLM.

    PROMPT mode uses ``compose`` (free-text prose). REFLECT mode uses
    ``compose_structured`` (Gemini ``response_schema``-constrained JSON
    via ``StructuredLLMClient``) per ADR 0040 §5. Callers wire one or
    the other based on ``input.mode``; an instance can carry both LLM
    surfaces and the agent picks per call.
    """

    def __init__(
        self,
        *,
        prompt_template: str,
        llm: LLMComposeClient,
        model: str = DEFAULT_MODEL,
        structured_llm: StructuredLLMClient | None = None,
    ) -> None:
        self._template = prompt_template
        self._llm = llm
        self._model = model
        self._structured_llm = structured_llm

    def compose(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        run_date: date,
        completed_tasks_block: str = "(none)",
        triaged_today_block: str = "(none)",
        calendar_block: str = "(none)",
        morning_brief_block: str = "(none)",
        active_risk_flags_block: str = "(none)",
        voice_memos_block: str = "(none)",
        in_flight_decisions_block: str = "(none)",
        open_followups_block: str = "(none)",
    ) -> str:
        prompt = self._template
        substitutions = {
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "run_date_human": run_date.strftime("%A, %B %d, %Y"),
            "completed_tasks_block": completed_tasks_block,
            "triaged_today_block": triaged_today_block,
            "calendar_block": calendar_block,
            "morning_brief_block": morning_brief_block,
            "active_risk_flags_block": active_risk_flags_block,
            "voice_memos_block": voice_memos_block,
            "in_flight_decisions_block": in_flight_decisions_block,
            "open_followups_block": open_followups_block,
        }
        for key, value in substitutions.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
        body = self._llm.compose(prompt=prompt, model=self._model).strip()
        return body or _unremarkable_day_fallback(run_date)

    def compose_doc(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        run_date: date,
        completed_tasks_block: str = "(none)",
        triaged_today_block: str = "(none)",
        calendar_block: str = "(none)",
        morning_brief_block: str = "(none)",
        active_risk_flags_block: str = "(none)",
        voice_memos_block: str = "(none)",
    ) -> ReflectExtractionPayload:
        """REFLECT-mode structured generation, Doc flavor (ADR 0044).

        Identical I/O surface to ``compose_structured`` (uses the
        ``StructuredLLMClient``), but the prompt template lives at
        ``prompts/evening_reflection/reflect_doc_v1.md`` and the LLM is
        instructed to emit ``custom_questions`` alongside the existing
        commentary + decisions/wins/todos arrays. The composer's own
        ``self._template`` is what gets substituted — callers wiring a
        Doc-mode composer pass the doc template at construction time.

        Returns ``ReflectExtractionPayload`` with ``custom_questions``
        populated; the caller renders it via
        ``render_reflection_doc_body_html`` along with the standard
        questions YAML.
        """
        return self._compose_structured_internal(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            run_date=run_date,
            completed_tasks_block=completed_tasks_block,
            triaged_today_block=triaged_today_block,
            calendar_block=calendar_block,
            morning_brief_block=morning_brief_block,
            active_risk_flags_block=active_risk_flags_block,
            voice_memos_block=voice_memos_block,
        )

    def compose_structured(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        run_date: date,
        completed_tasks_block: str = "(none)",
        triaged_today_block: str = "(none)",
        calendar_block: str = "(none)",
        morning_brief_block: str = "(none)",
        active_risk_flags_block: str = "(none)",
        voice_memos_block: str = "(none)",
    ) -> ReflectExtractionPayload:
        """REFLECT-mode structured generation (ADR 0040 §5).

        Substitutes the same blocks as ``compose`` into the reflect
        prompt template, calls the structured LLM client, and returns
        a ``ReflectExtractionPayload``. Raises ``structured_compose.ParseError``
        when the model output doesn't satisfy the schema — the agent
        catches and falls back to a prose-only draft so the daily
        ritual still ships.
        """
        return self._compose_structured_internal(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            run_date=run_date,
            completed_tasks_block=completed_tasks_block,
            triaged_today_block=triaged_today_block,
            calendar_block=calendar_block,
            morning_brief_block=morning_brief_block,
            active_risk_flags_block=active_risk_flags_block,
            voice_memos_block=voice_memos_block,
        )

    def _compose_structured_internal(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        run_date: date,
        completed_tasks_block: str,
        triaged_today_block: str,
        calendar_block: str,
        morning_brief_block: str,
        active_risk_flags_block: str,
        voice_memos_block: str,
    ) -> ReflectExtractionPayload:
        if self._structured_llm is None:
            raise RuntimeError(
                "structured composition requires a structured_llm — pass one "
                "to EveningReflectionComposer.__init__"
            )
        prompt = self._template
        substitutions = {
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "run_date_human": run_date.strftime("%A, %B %d, %Y"),
            "completed_tasks_block": completed_tasks_block,
            "triaged_today_block": triaged_today_block,
            "calendar_block": calendar_block,
            "morning_brief_block": morning_brief_block,
            "active_risk_flags_block": active_risk_flags_block,
            "voice_memos_block": voice_memos_block,
        }
        for key, value in substitutions.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
        raw = self._structured_llm.generate(prompt=prompt, model=self._model)
        return parse_extraction_payload(raw)


def _unremarkable_day_fallback(run_date: date) -> str:
    """If the LLM returns empty (rare), don't break the daily ritual.

    Spec §5.5: "If you have no genuine insight to offer, say 'today was
    a normal day, here's what got done' and stop." This fallback is the
    deterministic version of that — a single short paragraph, not the
    three-section structure, so the artifact's tone holds even on quiet
    days.
    """
    return (
        f"Today, {run_date.strftime('%A, %B %d')}, was an unremarkable day. "
        "Nothing in particular came through the inbox or the calendar that "
        "is worth surfacing here. The reflection picks up again tomorrow."
    )


def render_section_blocks(
    *,
    completed_tasks: list | None = None,
    triaged_today: list | None = None,
    calendar_events: list | None = None,
    morning_brief: object | None = None,
    active_risk_flags: list | None = None,
    timezone: str = "America/Los_Angeles",
    voice_memos: list | None = None,
    in_flight_decisions: list | None = None,
    open_followups: list | None = None,
) -> dict[str, str]:
    """Render each input list into a textual block for the prompt.

    Empty / missing blocks render as ``(none)`` so the LLM can apply its
    "produce the unremarkable-day fallback when everything is empty"
    rule cleanly. ADR 0040 §1 — both modes call this helper; each mode
    only populates the params relevant to its prompt template, the rest
    default to ``None``.
    """
    return {
        "completed_tasks_block": _render_completed_tasks(completed_tasks or []),
        "triaged_today_block": _render_triaged_today(triaged_today or []),
        "calendar_block": _render_calendar(calendar_events or [], timezone),
        "morning_brief_block": _render_morning_brief(morning_brief),
        "active_risk_flags_block": _render_active_risk_flags(active_risk_flags or []),
        "voice_memos_block": _render_voice_memos(voice_memos or []),
        "in_flight_decisions_block": _render_in_flight_decisions(in_flight_decisions or []),
        "open_followups_block": _render_open_followups(open_followups or []),
    }


def _render_completed_tasks(tasks: list) -> str:
    if not tasks:
        return "(none)"
    lines = []
    for t in tasks:
        proj = f" / {t.project_name}" if getattr(t, "project_name", None) else ""
        lines.append(f"- {t.name}{proj}")
    return "\n".join(lines)


def _render_triaged_today(items: list) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        url_suffix = f" — {it.source_url}" if getattr(it, "source_url", None) else ""
        action = getattr(it, "action_type", None) or ""
        action_suffix = f", action={action}" if action else ""
        lines.append(
            f"- [{it.severity}] {it.summary} (source={it.source}{action_suffix})" f"{url_suffix}"
        )
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
        lines.append(f"- {start_local}-{end_local}: {e.summary}{attendees_suffix}")
    return "\n".join(lines)


def _render_morning_brief(brief: object | None) -> str:
    """Surface today's morning brief as a block the LLM can compare execution against.

    The whole markdown body is included verbatim — it's already short
    (~300 words). When no brief was generated today (test/dev or first
    day on the system), render ``(none)``.
    """
    if brief is None:
        return "(none)"
    body = getattr(brief, "body_markdown", "") or ""
    body = body.strip()
    if not body:
        return "(none)"
    return body


def _render_active_risk_flags(flags: list) -> str:
    if not flags:
        return "(none)"
    lines = []
    for f in flags:
        acct = f" on {f.account_name}" if getattr(f, "account_name", None) else ""
        reason = f.reasoning or ""
        lines.append(f"- [{f.severity}] {f.pattern_name}{acct}: {reason}")
    return "\n".join(lines)


def _render_voice_memos(memos: list) -> str:
    """Render today's voice memos as one indented block per memo (ADR 0040 §3).

    The full transcript is included verbatim so the LLM can extract
    decisions / wins / todos from it (PR-B). Each memo is fenced with
    its ``note_id`` for provenance — the prompt instructs the model to
    attach this id to extracted rows when applicable.
    """
    if not memos:
        return "(none)"
    blocks: list[str] = []
    for m in memos:
        ts = ""
        ingested = getattr(m, "ingested_at", None)
        if ingested is not None:
            try:
                ts = f" (ingested {ingested.strftime('%H:%M UTC')})"
            except Exception:
                ts = ""
        body = (getattr(m, "markdown_content", "") or "").strip() or "(empty transcript)"
        blocks.append(f"[note_id={m.note_id}{ts}]\n{body}")
    return "\n\n".join(blocks)


def _render_in_flight_decisions(decisions: list) -> str:
    """Render in-flight decisions as a bullet list for PROMPT-mode anchor."""
    if not decisions:
        return "(none)"
    lines: list[str] = []
    for d in decisions:
        title = getattr(d, "title", "") or "(untitled)"
        status = getattr(d, "status", "") or ""
        ctx = (getattr(d, "context", None) or "").strip()
        ctx_suffix = f" — {ctx}" if ctx else ""
        lines.append(f"- [{status}] {title}{ctx_suffix}")
    return "\n".join(lines)


def render_reflection_doc_body_html(
    *,
    recipient_name: str,
    run_date: date,
    payload: ReflectExtractionPayload,
    standard_questions: StandardQuestions | None,
    completed_tasks_block: str = "(none)",
    triaged_today_block: str = "(none)",
    calendar_block: str = "(none)",
    morning_brief_block: str = "(none)",
    active_risk_flags_block: str = "(none)",
    voice_memos_block: str = "(none)",
    areas_context: tuple[AreaNoteSnippet, ...] | None = None,
) -> str:
    """Compose the full HTML body for the Reflection Doc (ADR 0044 §6).

    Drive auto-converts ``mimeType=application/vnd.google-apps.document``
    uploads when the body is HTML — headings, bold, bullets all land
    natively. We emit a small, minimal HTML document (no <html>/<body>
    wrappers required; Drive accepts a fragment).

    The body is *deterministic*: only ``payload.commentary``,
    ``payload.custom_questions``, and the structured-extraction summary
    come from the LLM. Standard questions, today's-signals sections, and
    the empty "Your reflection" anchor are rendered server-side.
    """
    parts: list[str] = []
    parts.append(f"<h1>Evening Reflection — {html.escape(run_date.strftime('%A, %B %d, %Y'))}</h1>")
    parts.append(f"<p><i>For {html.escape(recipient_name)}.</i></p>")

    parts.append("<h2>Today's signals</h2>")
    parts.append(_signals_block_html("Tasks completed today", completed_tasks_block))
    parts.append(_signals_block_html("Triaged items today", triaged_today_block))
    parts.append(_signals_block_html("Calendar attended today", calendar_block))
    parts.append(_signals_block_html("Today's morning brief", morning_brief_block))
    parts.append(_signals_block_html("Active risk flags", active_risk_flags_block))

    parts.append("<h2>Reflection commentary</h2>")
    parts.append(_markdown_paragraphs_to_html(payload.commentary))

    parts.append("<h2>Standard reflection questions</h2>")
    parts.append(_render_questions_html(standard_questions))

    parts.append("<h2>Custom reflection questions</h2>")
    parts.append(_render_custom_questions_html(payload.custom_questions))

    parts.append("<h2>Voice memo extracts</h2>")
    parts.append(_render_extraction_summary_html(payload))

    parts.append("<h2>Areas context</h2>")
    parts.append(_render_areas_context_html(areas_context))

    parts.append("<h2>Voice memos</h2>")
    parts.append(_signals_block_html(None, voice_memos_block))

    parts.append("<h2>Your reflection</h2>")
    parts.append("<p><i>(Type below — this is your space.)</i></p>")
    parts.append("<p></p>")
    return "\n".join(parts)


def _signals_block_html(label: str | None, body: str) -> str:
    """Render a (label, body) pair as an HTML subsection.

    Empty / "(none)" bodies render as a one-liner so the Doc reads
    cleanly on quiet days.
    """
    label_html = f"<h3>{html.escape(label)}</h3>" if label else ""
    body_str = (body or "").strip()
    if not body_str or body_str == "(none)":
        return f"{label_html}<p>(none)</p>"
    if "\n" in body_str:
        # Bullet-list style — each line is one item. Already-prefixed
        # "- " bullets are stripped so we don't render two markers.
        lines = [ln.strip() for ln in body_str.split("\n") if ln.strip()]
        items = []
        for ln in lines:
            stripped = ln.lstrip("-• ").strip()
            items.append(f"<li>{html.escape(stripped)}</li>")
        return f"{label_html}<ul>{''.join(items)}</ul>"
    return f"{label_html}<p>{html.escape(body_str)}</p>"


def _markdown_paragraphs_to_html(markdown: str) -> str:
    """Best-effort markdown → HTML for ``commentary`` (no full parser).

    The LLM's commentary is mostly ``#### subhead`` + paragraph blocks.
    This minimal converter covers the cases the prompt produces:
      - lines starting with ``#### `` → <h4>
      - lines starting with ``### `` → <h3>
      - lines starting with ``## `` → <h2> (rare)
      - blank-line separated paragraphs → <p>
    Anything else is treated as paragraph text. HTML special chars are
    escaped; literal Markdown emphasis (``**bold**``, ``_italic_``) is
    left as-is — Google Docs renders them cleanly enough on
    auto-convert from HTML, and a full Markdown→HTML pass isn't worth
    a new dependency for a daily Doc.
    """
    text = (markdown or "").strip()
    if not text:
        return "<p>(no commentary)</p>"
    out: list[str] = []
    paragraph: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            if paragraph:
                out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
                paragraph = []
            continue
        if line.startswith("#### "):
            if paragraph:
                out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
                paragraph = []
            out.append(f"<h4>{html.escape(line[5:].strip())}</h4>")
            continue
        if line.startswith("### "):
            if paragraph:
                out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
                paragraph = []
            out.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
            continue
        if line.startswith("## "):
            if paragraph:
                out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
                paragraph = []
            out.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
            continue
        paragraph.append(line)
    if paragraph:
        out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
    return "\n".join(out) or "<p>(no commentary)</p>"


def _render_questions_html(qs: StandardQuestions | None) -> str:
    if qs is None:
        return "<p>(standard questions config not loaded)</p>"
    selected = qs.select()
    if not selected:
        return "<p>(no standard questions configured)</p>"
    items = [f"<li>{html.escape(q.prompt)}</li>" for q in selected]
    return f"<ol>{''.join(items)}</ol>"


def _render_custom_questions_html(custom: tuple[str, ...]) -> str:
    cleaned = [c.strip() for c in custom if c and c.strip()]
    if not cleaned:
        return "<p>(none surfaced today)</p>"
    items = [f"<li>{html.escape(q)}</li>" for q in cleaned]
    return f"<ol>{''.join(items)}</ol>"


def _render_extraction_summary_html(payload: ReflectExtractionPayload) -> str:
    """Mirror ``reflect_dispatch.render_dispatch_summary_block`` shape but as HTML.

    Renders a short table-of-extracts so the user can see what was lifted
    from voice memos and now lives in ``agent_outputs.{decisions,wins}``.
    Empty everything → "(no decisions, wins, or todos extracted today)".
    """
    if not payload.decisions and not payload.wins and not payload.todos:
        return "<p>(no decisions, wins, or todos extracted today)</p>"
    parts: list[str] = []
    if payload.decisions:
        parts.append("<h4>Decisions</h4>")
        parts.append("<ul>")
        for d in payload.decisions:
            ctx = f" — {html.escape(d.context)}" if d.context else ""
            parts.append(f"<li><b>{html.escape(d.title)}</b>{ctx}</li>")
        parts.append("</ul>")
    if payload.wins:
        parts.append("<h4>Wins</h4>")
        parts.append("<ul>")
        for w in payload.wins:
            summ = f" — {html.escape(w.summary)}" if w.summary else ""
            parts.append(f"<li><b>{html.escape(w.title)}</b>{summ}</li>")
        parts.append("</ul>")
    if payload.todos:
        parts.append("<h4>Todos</h4>")
        parts.append("<ul>")
        for t in payload.todos:
            parts.append(f"<li>{html.escape(t.body)}</li>")
        parts.append("</ul>")
    return "\n".join(parts)


def _render_areas_context_html(areas: tuple[AreaNoteSnippet, ...] | None) -> str:
    """Render the top-K Areas context snippets (ADR 0038 §5).

    Empty / no corpus / disabled (None) → "(none surfaced)" so the Doc
    section doesn't read awkwardly. Each snippet links to the source
    Drive file when ``source_drive_url`` is populated.
    """
    if not areas:
        return "<p>(none surfaced)</p>"
    items: list[str] = []
    for note in areas:
        title = html.escape(note.filename) if note.filename else "(untitled)"
        if note.source_drive_url:
            title = f'<a href="{html.escape(note.source_drive_url)}">{title}</a>'
        snippet = html.escape(note.snippet) if note.snippet else "(empty)"
        # Distance is debug-grade — surface it small so users know which
        # neighbor is the closest match without it dominating the layout.
        items.append(
            f"<li><b>{title}</b><br/>"
            f"<span>{snippet}</span><br/>"
            f"<small><i>distance {note.distance:.3f}</i></small></li>"
        )
    return f"<ul>{''.join(items)}</ul>"


_ACTIONABLE_ACTIONS = frozenset({"do_now", "defer", "schedule", "wait"})


def _render_open_followups(items: list) -> str:
    """Render the actionable subset of triaged items for PROMPT-mode anchor.

    ADR 0040 §10 — same data the REFLECT-mode triaged block uses, but
    filtered in-memory to actionable action_types. Pure-info items get
    surfaced in REFLECT-mode prose, not the PROMPT-mode anchor.
    """
    if not items:
        return "(none)"
    actionable = [
        it for it in items if (getattr(it, "action_type", "") or "").lower() in _ACTIONABLE_ACTIONS
    ]
    if not actionable:
        return "(none)"
    lines: list[str] = []
    for it in actionable:
        action = (getattr(it, "action_type", "") or "").lower()
        url_suffix = f" — {it.source_url}" if getattr(it, "source_url", None) else ""
        lines.append(f"- [{it.severity}/{action}] {it.summary} (source={it.source}){url_suffix}")
    return "\n".join(lines)
