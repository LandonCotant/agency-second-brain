"""MCP server entry point — registers the 17 v0.1 tools with FastMCP.

ADR 0051 §2. Local stdio transport. Auth via operator ADC; no SA
key. Tool-level safety wrappers live in ``tools/read.py`` and
``tools/write.py``.

To run:

    pip install -e ".[mcp-server]"
    asb-mcp-server      # stdio mode, expects an MCP client to drive it

Claude Desktop config (``~/Library/Application Support/Claude/claude_desktop_config.json``):

    {
      "mcpServers": {
        "brain": {
          "command": "asb-mcp-server",
          "env": {"BRAIN_PROJECT_ID": "agency-brain-demo"}
        }
      }
    }
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .tools.docs import update_weekly_doc
from .tools.person import pending_followups, person_summary, sync_people
from .tools.read import (
    brain_ask,
    client_summary,
    entity_facts,
    get_calendar_events,
    open_commitments,
    open_drafts,
    open_risk_flags,
    related_notes,
)
from .tools.write import (
    capture_note,
    insert_decision,
    insert_win,
    mark_decision_status,
    record_feedback,
)

mcp = FastMCP(
    "brain",
    instructions=(
        "Tools for the Agency Second Brain — agency-specific signal substrate "
        "(ADR 0051). Use `brain_ask` for free-text semantic recall over "
        "the corpus (emails, calendar, drive notes, captures). Use "
        "`client_summary` for structured client briefings — prefer it over "
        "`brain_ask` for 'who is X' / 'brief me on Y' questions. Use "
        "`open_risk_flags` for anything about what's flagged, who's at "
        "risk, or who needs attention right now — not `brain_ask`. Use "
        "`open_drafts` for 'what's queued for me?' / 'what needs my "
        "approval?' — surfaces Airtable Tasks the agents have drafted "
        "but you haven't yet confirmed. Use `pending_followups` for "
        "'who am I behind on?' / 'anyone overdue?' — surfaces Contacts "
        "whose next_followup is due today or earlier (pass "
        "`window_days=7` for the next-week view). Use "
        "`get_calendar_events` for any time-bound question about the "
        "calendar (this week / next week / a specific date / 'what's "
        "coming up') — `brain_ask` under-weights events with sparse text "
        "like birthdays, so time-bound queries need the deterministic "
        "enumeration. Two-phase pattern works well for week planning: "
        "call `get_calendar_events` first for the skeleton, then "
        "`brain_ask` for context. Use `related_notes(note_id)` for "
        "graph traversal — what's directly connected to a known note. "
        "Pairs naturally with `brain_ask` (find a note semantically, "
        "then walk its neighbors structurally). Returns both semantic "
        "(Librarian VECTOR_SEARCH) and user-typed `[[X]]` wikilink "
        "edges. "
        "Use `person_summary` for structured briefings on a specific "
        "**human** (a contact, classmate, mentor) — prefer it over "
        "`client_summary` (which is for accounts/companies) and over "
        "`brain_ask` for 'who is X' / 'brief me on X' of a named "
        "person (ADR 0057). Pulls Airtable warmth + last_contact + "
        "next_followup + recent calendar attendance in one call. "
        "Use `sync_people` to manually refresh the Brain's people "
        "notes from Airtable after a CRM edit — triggers asb-people-sync "
        "on demand (otherwise weekly Sunday). "
        "`capture_note` is the 'remember this' write path. "
        "`update_weekly_doc` is the deposit path for scheduled routines' "
        "Morning Brief / Evening Reflection output — it prepends today's "
        "section to the current week's consolidated Doc instead of "
        "creating a new file every day. The other write "
        "tools (`insert_decision`, `insert_win`, `mark_decision_status`) "
        "are primarily for scheduled Claude routines, not chat — see each "
        "tool's docstring for the narrow exceptions. "
        "Use `record_feedback` when the operator reacts to a flag/draft with "
        "a judgment to remember — 'that flag is noise', 'mute <account> for a "
        "month', 'good catch', 'wrong tone'. A 'noise' verdict suppresses "
        "future emissions of that (account, pattern) flag (ADR 0060); the "
        "other verdicts are recorded for later tuning. Never invent "
        "`decision_id`, `note_id`, or `account_id` values — only reference "
        "IDs returned from a prior tool call in the same conversation. "
        "Write tools require explicit user intent ('remember this', 'log "
        "this decision') — don't write speculatively. For raw "
        "Gmail/Drive/Calendar/Airtable reads, prefer the dedicated "
        "`mcp__claude_ai_*` connectors over working around them through "
        "these tools."
    ),
)

# Read tools — all idempotent, read-only.
mcp.tool(
    annotations=ToolAnnotations(
        title="Brain semantic search",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(brain_ask)

mcp.tool(
    annotations=ToolAnnotations(
        title="Open client-risk flags",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(open_risk_flags)

mcp.tool(
    annotations=ToolAnnotations(
        title="Structured client briefing",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(client_summary)

mcp.tool(
    annotations=ToolAnnotations(
        title="Calendar events in date range",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(get_calendar_events)

mcp.tool(
    annotations=ToolAnnotations(
        title="Related notes (graph neighbors)",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(related_notes)

mcp.tool(
    annotations=ToolAnnotations(
        title="Open drafts awaiting my approval",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(open_drafts)

mcp.tool(
    annotations=ToolAnnotations(
        title="Pending followups (overdue + due-today contacts)",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(pending_followups)

mcp.tool(
    annotations=ToolAnnotations(
        title="Open commitments (overdue promises, mine + theirs)",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(open_commitments)

mcp.tool(
    annotations=ToolAnnotations(
        title="Entity facts (current attributes + history)",
        readOnlyHint=True,
        idempotentHint=True,
        destructiveHint=False,
    )
)(entity_facts)

# Write tools. ``destructiveHint`` is True only when the tool can
# modify (not just add) state. ``idempotentHint`` reflects whether
# repeated calls with the same args produce the same outcome — so the
# host LLM knows it's safe to retry on transient failure.

mcp.tool(
    annotations=ToolAnnotations(
        title="Capture note into corpus",
        readOnlyHint=False,
        destructiveHint=False,  # additive write only
        idempotentHint=True,  # SHA-256 dedup on text
    )
)(capture_note)

mcp.tool(
    annotations=ToolAnnotations(
        title="Mark decision confirmed/dismissed",
        readOnlyHint=False,
        destructiveHint=True,  # modifies a row's status field
        idempotentHint=True,  # WHERE status='drafted' guard → no-op on re-call
    )
)(mark_decision_status)

mcp.tool(
    annotations=ToolAnnotations(
        title="Insert drafted decision (routine)",
        readOnlyHint=False,
        destructiveHint=False,  # additive write only
        idempotentHint=False,  # each call generates a new decision_id
    )
)(insert_decision)

mcp.tool(
    annotations=ToolAnnotations(
        title="Insert win (routine)",
        readOnlyHint=False,
        destructiveHint=False,  # additive write only
        idempotentHint=True,  # title_hash12 dedup
    )
)(insert_win)

mcp.tool(
    annotations=ToolAnnotations(
        title="Record signal feedback (mute noise / tune)",
        readOnlyHint=False,
        destructiveHint=False,  # additive write only; suppression gate reads it
        idempotentHint=True,  # 60s tuple dedup
    )
)(record_feedback)

mcp.tool(
    annotations=ToolAnnotations(
        title="Prepend section to this week's Brief/Reflection doc",
        readOnlyHint=False,
        destructiveHint=False,  # additive prepend; no existing content is rewritten
        idempotentHint=True,  # same-day re-run detected, no double insert
    )
)(update_weekly_doc)

mcp.tool(
    annotations=ToolAnnotations(
        title="Person briefing (Airtable + Brain + recent activity)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    )
)(person_summary)

mcp.tool(
    annotations=ToolAnnotations(
        title="Manually fire asb-people-sync (refresh Brain people notes from Airtable)",
        readOnlyHint=False,
        destructiveHint=False,  # additive sync; never deletes Brain content
        idempotentHint=True,  # re-running the same Airtable state is a no-op
    )
)(sync_people)
