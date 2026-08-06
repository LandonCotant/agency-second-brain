// Remote brain MCP server on Cloudflare Workers (ADR 0067).
//
// OAuthProvider terminates OAuth 2.1 (+ DCR) for claude.ai custom
// connectors; GoogleHandler logs the user in against the email allowlist;
// BrainMCP (a Durable-Object-backed McpAgent) serves the tools. Data
// access uses the asb-mcp-sa key (see gcp/auth.ts), not the user's token —
// login is the authorization gate, the SA is the data-plane identity.

import OAuthProvider from "@cloudflare/workers-oauth-provider";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { McpAgent } from "agents/mcp";
import { z } from "zod";
import { GoogleHandler } from "./google-handler";
import * as read from "./tools/read";
import * as write from "./tools/write";
import * as person from "./tools/person";
import { updateWeeklyDoc } from "./tools/docs";
import type { Env, Props } from "./types";

const INSTRUCTIONS =
  "Tools for the Agency Second Brain — agency-specific signal substrate (ADR 0051). " +
  "Use `brain_ask` for free-text semantic recall over the corpus. Prefer " +
  "`client_summary` for 'who is X'/'brief me on Y'. Use `open_risk_flags` for " +
  "what's flagged / who's at risk. Use `open_drafts` for 'what needs my " +
  "approval?'. Use `get_calendar_events` for time-bound calendar questions " +
  "(brain_ask under-weights sparse events like birthdays). Use " +
  "`open_commitments` for 'what did I promise / what's overdue'. Use " +
  "`related_notes` to walk graph neighbors of a known note_id. Never invent " +
  "note_id/account_id values — only reference IDs returned earlier in the " +
  "conversation.";

function json(obj: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj) }] };
}

export class BrainMCP extends McpAgent<Env, Record<string, never>, Props> {
  server = new McpServer({ name: "brain", version: "0.1.0" }, { instructions: INSTRUCTIONS });

  async init() {
    const env = this.env;

    this.server.tool(
      "brain_ask",
      "Semantic + keyword search over the agency corpus (emails, calendar, " +
        "notes, captures, decisions, wins). Returns ranked chunks; you " +
        "synthesize. Use for 'what did X say about…', 'find the note where…', " +
        "'what do I know about…'. NOT for 'what's flagged' (open_risk_flags) " +
        "or 'tell me about <client>' (client_summary). HIPAA material excluded.",
      { query: z.string(), max_results: z.number().int().min(1).max(50).default(8) },
      async ({ query, max_results }) => json(await read.brainAsk(env, query, max_results)),
    );

    this.server.tool(
      "open_risk_flags",
      "Currently-open client risk flags (resolved_at IS NULL), newest first. " +
        "Use for 'what's flagged right now', 'any client risks', 'how's " +
        "<segment> doing'. No write/resolve path here.",
      {
        segment: z
          .enum(["E-commerce", "Local Service", "Agency Partner", "Personal"])
          .optional(),
        min_severity: z.enum(["low", "medium", "high", "critical"]).optional(),
      },
      async ({ segment, min_severity }) =>
        json(await read.openRiskFlags(env, segment, min_severity)),
    );

    this.server.tool(
      "client_summary",
      "One-shot structured briefing on a named client: canonical Account row " +
        "+ open risks + recent triaged items + recent meetings/emails. Prefer " +
        "over brain_ask for 'who is X'/'brief me on <client>'. Case-insensitive " +
        "substring match on company name.",
      { account_name: z.string() },
      async ({ account_name }) => json(await read.clientSummary(env, account_name)),
    );

    this.server.tool(
      "get_calendar_events",
      "Enumerate calendar events in [start_date, end_date) — deterministic, no " +
        "semantic ranking. Use for 'what's on my calendar this week', 'meetings " +
        "on <date>', 'when is <event>'. Dates are ISO YYYY-MM-DD; end_date is " +
        "exclusive. scope is 'agency' or 'personal' ('work' aliases 'agency').",
      {
        start_date: z.string(),
        end_date: z.string(),
        scope: z.enum(["agency", "personal", "work"]).optional(),
        include_all_day: z.boolean().default(true),
      },
      async ({ start_date, end_date, scope, include_all_day }) =>
        json(await read.getCalendarEvents(env, start_date, end_date, scope, include_all_day)),
    );

    this.server.tool(
      "open_drafts",
      "Airtable Tasks awaiting your approval (approval_status='Drafted by " +
        "Agent'), newest first. Use for 'what's queued for me', 'what needs my " +
        "approval', 'anything to review'.",
      { limit: z.number().int().min(1).max(100).default(20) },
      async ({ limit }) => json(await read.openDrafts(env, limit)),
    );

    this.server.tool(
      "open_commitments",
      "Open, due/overdue commitments extracted from the corpus (ADR 0069). Use " +
        "for 'what did I say I'd do', 'what's overdue', 'what's <person> supposed " +
        "to send me'. direction: 'mine' | 'theirs' | omitted (both). " +
        "days_overdue=0 means due-today-or-overdue.",
      {
        direction: z.enum(["mine", "theirs"]).optional(),
        account_name: z.string().optional(),
        days_overdue: z.number().int().min(0).default(0),
      },
      async ({ direction, account_name, days_overdue }) =>
        json(await read.openCommitments(env, direction, account_name, days_overdue)),
    );

    this.server.tool(
      "related_notes",
      "Graph-walk: notes directly linked to note_id via notes_links — both " +
        "semantic (Librarian) and user-typed [[wikilink]] edges (ADR 0053). " +
        "Pairs with brain_ask (find a note semantically, then walk neighbors). " +
        "Pass link_types ['wikilink'] to restrict. Depth 1 only.",
      {
        note_id: z.string(),
        link_types: z.array(z.string()).optional(),
        limit: z.number().int().min(1).max(100).default(20),
      },
      async ({ note_id, link_types, limit }) =>
        json(await read.relatedNotes(env, note_id, link_types, limit)),
    );

    this.server.tool(
      "pending_followups",
      "Airtable Contacts whose next_followup is due/overdue. Use for 'who am " +
        "I behind on', 'anyone overdue', 'my followup list this week' " +
        "(window_days=7). window_days=0 (default) = due today or overdue.",
      {
        window_days: z.number().int().min(0).max(60).default(0),
        limit: z.number().int().min(1).max(100).default(20),
      },
      async ({ window_days, limit }) => json(await person.pendingFollowups(env, window_days, limit)),
    );

    this.server.tool(
      "person_summary",
      "Structured briefing on a specific human (contact) across Airtable + " +
        "Brain: role, org, warmth, last_contact, next_followup, recent calendar " +
        "activity. Prefer over client_summary (companies) and brain_ask for " +
        "'who is <person>' / 'brief me on <person>'. Match by name OR email.",
      { name_or_email: z.string() },
      async ({ name_or_email }) => json(await person.personSummary(env, name_or_email)),
    );

    // --- Write tools. Require explicit user intent ('remember this', 'log
    // this decision'); never write speculatively. ---

    this.server.tool(
      "capture_note",
      "Capture an ad-hoc thought into the corpus for later brain_ask recall. " +
        "Use for 'remember that…', 'capture this:', 'note that…', 'don't let me " +
        "forget…'. Idempotent on SHA-256 of the text. NOT for long docs (use " +
        "Drive), emails/tasks (use those connectors), or reflection extracts.",
      { text: z.string(), source_hint: z.string().optional() },
      async ({ text, source_hint }) => json(await write.captureNote(env, text, source_hint)),
    );

    this.server.tool(
      "record_feedback",
      "Record the operator's verdict on a signal so agents stop re-surfacing " +
        "noise (ADR 0060). Use for 'that <account> <pattern> flag is noise', " +
        "'mute <account> for a month', 'good catch', 'wrong tone'. Only " +
        "verdict='noise' suppresses future emissions. pattern_name must match " +
        "open_risk_flags output verbatim (e.g. 'Owner Disengagement'); omit to " +
        "mute all patterns on the account.",
      {
        scope: z.enum(["risk", "triage", "draft"]),
        verdict: z.enum(["noise", "valid", "wrong_tone", "wrong_target"]),
        account_name: z.string().optional(),
        pattern_name: z.string().optional(),
        note: z.string().optional(),
        mute_days: z.number().int().min(1).optional(),
        source_flag_id: z.string().optional(),
      },
      async (args) => json(await write.recordFeedback(env, args)),
    );

    this.server.tool(
      "mark_decision_status",
      "Transition a drafted decision to confirmed or dismissed. Drafts-only " +
        "guard: WHERE status='drafted', so already-transitioned rows are a " +
        "no-op (returns updated=false + prior_status). Use for 'confirm/dismiss " +
        "decision <id>'. NOT for creating decisions (insert_decision) or tasks " +
        "(Airtable).",
      { decision_id: z.string(), status: z.enum(["confirmed", "dismissed"]) },
      async ({ decision_id, status }) => json(await write.markDecisionStatus(env, decision_id, status)),
    );

    this.server.tool(
      "insert_decision",
      "Insert a drafted decision row (status forced to 'drafted'; ADR 0052 " +
        "synthetic note for brain_ask). PRIMARILY FOR SCHEDULED ROUTINES — from " +
        "chat prefer capture_note unless the user explicitly says 'log this as a " +
        "decision I need to confirm'.",
      { text: z.string(), source: z.string().optional() },
      async ({ text, source }) => json(await write.insertDecision(env, text, source)),
    );

    this.server.tool(
      "insert_win",
      "Insert a win row (title_hash12 dedup per ADR 0043; ADR 0052 synthetic " +
        "note). PRIMARILY FOR SCHEDULED ROUTINES — from chat prefer capture_note " +
        "unless the user explicitly says 'log this as a win' / 'track for Brag " +
        "Spotter'.",
      { title: z.string(), context: z.string().optional(), source_id: z.string().optional() },
      async ({ title, context, source_id }) => json(await write.insertWin(env, title, context, source_id)),
    );

    this.server.tool(
      "sync_people",
      "Manually fire the asb-people-sync Cloud Run Job (refreshes Brain people " +
        "notes from Airtable). Use after editing Airtable contacts/accounts: " +
        "'sync the people notes', 'refresh the personal CRM'. Otherwise runs " +
        "Sundays. Async — returns the operation, not a finished run.",
      { region: z.enum(["us-central1", "us-east1", "us-east4", "us-west1"]).default("us-central1") },
      async ({ region }) => json(await person.syncPeople(env, region)),
    );

    this.server.tool(
      "update_weekly_doc",
      "Prepend today's section to the consolidated Brief/Reflection/Review " +
        "Google Doc (one per week per kind; reviews quarterly). PRIMARILY FOR " +
        "SCHEDULED ROUTINES depositing composed output — NOT ad-hoc notes " +
        "(capture_note) or free-form docs (Drive MCP). Idempotent on (kind, " +
        "date); resolves [[wikilinks]] to Galaxy Doc links.",
      {
        kind: z.enum(["brief", "reflection", "review"]),
        date: z.string(),
        section_markdown: z.string(),
        section_label: z.string().optional(),
      },
      async ({ kind, date, section_markdown, section_label }) =>
        json(await updateWeeklyDoc(env, kind, date, section_markdown, section_label)),
    );
  }
}

export default new OAuthProvider({
  apiHandler: BrainMCP.serve("/mcp") as never,
  apiRoute: "/mcp",
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
  defaultHandler: GoogleHandler as never,
});
