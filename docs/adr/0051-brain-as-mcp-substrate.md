# ADR 0051 — Brain is a signal substrate; conversation moves to MCP

**Status:** Accepted — 2026-05-13. Amended 2026-05-14 (§3 narrowed —
push artifacts and structured-signal generators removed from the
deprecation list; only Surfacer synthesis + Chat surface remain).
Extends ADR 0050 (Brain API surface). Amends ADR 0046 (Knowledge
Surfacer model + surface).

## Context

Phase H (ADRs 0045–0050) closed out the agent build-out: 14 Cloud Run
jobs/services, 50 ADRs, ~13.8k LOC of code. Looking at what was built,
the system has two distinct functions:

1. **Signal generation** — Triage classifies inbound, Risk Watcher
   accumulates longitudinal multi-segment signals (Acknowledgment Gap,
   Owner Disengagement v2, Personal Re-Engagement), Notes/Calendar/CRM
   ingesters fill the corpus, Librarian classifies + dossier-links
   dropped files, CRM Auto-updater drafts Tasks + Pending Updates from
   `secondbrain`-labeled email, Captures Materializer turns Airtable
   form submissions into corpus rows, routing fan-out pushes alerts.
   These are all backed by longitudinal state in `risk_flags`,
   `triaged_items`, `decisions`, `wins`, `notes`, `notes_links`,
   `crm_updater_runs`, and the HIPAA-cascade machinery.

2. **Narrative composition** — Morning Brief composes a daily prose
   brief from triaged_items + risk_flags + tasks + calendar; Evening
   Reflection composes a Reflection Doc with 5-question template +
   voice-memo extracts + Areas/Clients context; Brag Spotter composes
   a weekly wins summary; Knowledge Surfacer's `gemini-2.5-flash`
   synthesis step composes prose answers from VECTOR_SEARCH chunks.

By early-2026, off-the-shelf agent tools (Claude with MCP, ChatGPT with
Connectors, Cowork-class products) have commoditized the conversation
+ narrative-composition layer. Given any structured input, an LLM with
tool calling composes prose well enough that pre-composing narrative
behind the scenes is duplicative work.

ADR 0050 already positioned the Brain as a callable backend via
`POST /api/ask`. This ADR extends that direction: the Brain's identity
is the signal layer + writeback substrate, NOT a chat product. The
conversation layer lives outside the Brain, behind an MCP seam.

## Decision

### §1 — Brain identity: signal substrate, not a chat product

The Brain owns:

- **Longitudinal signal generation** that requires running on a
  schedule against historical data (Risk Watcher's "silent for N
  days," Brag Spotter's source aggregation, Triage's classification
  with goal-hierarchy weighting).
- **Push events** that fire on data arrival, not on user prompt
  (routing fan-out to Chat + Gmail draft, CRM Auto-updater drafting
  Tasks when emails arrive).
- **Drafts-only writeback orchestration** (PRD §4.7) — when to draft
  an Airtable Task vs append a Contacts/Accounts Pending Updates
  block vs create a Reflection Doc. The credentials, routing logic,
  and idempotency are all in this layer.
- **HIPAA cascade machinery** (PRD §4.1) — propagation of
  `Accounts.HIPAA` through the Lookup chain, filter formulas in
  `hipaa_filters.py`, attendee-domain HIPAA gating in the ingesters.
- **Ingestion + the corpus** — `agent_outputs.notes` populated by
  Notes Ingestor, Librarian, Calendar Ingester, CRM Auto-updater,
  Captures Materializer. The retrieval primitive
  (`VECTOR_SEARCH` over `agent_outputs.notes` with the ADR 0045 §9
  length-pre-filter) stays.

The Brain does NOT own:

- The conversation UI (Chat card surfaces, slash commands, voice
  clients, the existing Workspace Add-ons surface). These are
  consumers of the Brain, not part of it.
- Narrative composition of pre-computed prose for human consumption.
- General-purpose corpus + retrieval as a product (off-the-shelf
  tools provide this; the Brain's corpus is *agency-specific*).

### §2 — Expose Brain via MCP

A new project `mcp-server-brain` exposes Brain capabilities as MCP
tools that any MCP-speaking client (Claude Desktop, ChatGPT, local
models with MCP bridges, the Agent Coordination dashboard's Gemini
Live) can call.

Initial tool set (refined during implementation):

| Tool | Reads | Writes | Notes |
|---|---|---|---|
| `brain_ask(query, max_results)` | `/api/ask` (ADR 0050) | — | Retrieval over `agent_outputs.notes` |
| `open_risk_flags(segment?, min_severity?)` | `agent_outputs.risk_flags` WHERE resolved_at IS NULL | — | Active client risk |
| `drafted_tasks_for_review(limit?)` | Airtable Tasks WHERE Approval Status = "Drafted by Agent" | — | Human-review queue |
| `this_week_wins()` | `agent_outputs.wins` last 7d | — | Brag Spotter inputs |
| `recent_decisions(status?)` | `agent_outputs.decisions` last 30d | — | Captures/Reflection outputs |
| `client_summary(account_name)` | `airtable_replica.accounts` + active project + recent triaged_items + recent risk_flags | — | One-shot client briefing |
| `recent_triaged_items(severity?, lookback_hours?)` | `agent_outputs.triaged_items` | — | Inbound classification |
| `capture_note(text, source?)` | — | INSERT `agent_outputs.notes` `(note_kind='capture', scope='personal')` + embed | Mobile/voice capture surface, replaces the Airtable Captures form for ad-hoc use |
| `draft_gmail_reply(thread_id, body)` | — | Creates Gmail draft via existing `asb-agent-triage-sa` `gmail.compose` DWD | Reuses ADR 0027 + 0032 mechanism |
| `mark_decision_status(decision_id, status)` | — | UPDATE `agent_outputs.decisions` SET status = ... (mirrors the AI Ops Dashboard pattern from PR #128) | Drafts → confirmed/dismissed |

Authentication: the MCP server runs locally (stdio transport) and
accesses GCP via the operator's ADC. No new SA. Reuses existing IAM
(operator already has impersonation of `asb-agent-triage-sa` per
ADR 0027 + 0032 token-creator binding).

Idempotency: writes (`capture_note`, `draft_gmail_reply`,
`mark_decision_status`) MERGE/UPSERT or no-op on duplicate; the LLM
can retry without side effects.

### §3 — Deprecation list, narrowed (amended 2026-05-14)

**Amendment context.** The original §3 (as merged in PR #129) flagged
Morning Brief, Evening Reflection (both modes), Brag Spotter, the
Knowledge Surfacer synthesis step, and the Workspace Add-ons Chat
surface for deprecation. That was a category error — scheduled push
artifacts and structured-signal generators were lumped together with
request-response narrative composition. After the 2026-05-14 review
the deprecation list is narrowed to two items only.

**Push artifacts and structured-signal generators STAY** (originally
flagged here, removed from the cut list):

- **Morning Brief** (ADR 0029) stays. Value is the *cadence* — 7:25am
  PT Gmail draft landing during coffee without being asked. A
  Claude-on-demand replacement would require the operator to
  initiate the interaction, defeating the daily-orientation purpose.
- **Evening Reflection** PROMPT mode (4pm) and REFLECT mode (9pm)
  both stay (ADR 0040, ADR 0044). PROMPT is the same push-artifact
  argument. REFLECT additionally (a) creates a dated Google Doc in
  `Brain/Areas/Reflections/` — a persistent journal artifact a
  conversation surface can't replicate, and (b) writes structured
  rows into `agent_outputs.decisions` and `agent_outputs.wins` from
  voice-memo extraction; those are *upstream* of `brain_ask`, not
  redundant with it.
- **Brag Spotter** (ADR 0043) stays. The aggregation produces `wins`
  rows that surface patterns over a 7-day window; Claude-on-demand
  doesn't accumulate. The weekly summary post is the artifact, the
  rows are the signal.

**Genuinely on the cut list** (both request-response narrative over
already-retrieved data — exactly the pattern Claude-on-demand
replaces):

- **Knowledge Surfacer `gemini-2.5-flash` synthesis step**
  (ADR 0046 §5). The retrieval pipeline (embed → VECTOR_SEARCH →
  app-layer guardrail) stays as `/api/ask` / `brain_ask`. The
  synthesis step on top of retrieved chunks is what the conversation
  layer does for free. Cut when the MCP path is shipped + Claude
  Desktop consumption is verified.
- **Workspace Add-ons Chat surface** (ADR 0046 §3, PR #118). Works
  today and stays running until the MCP path is the verified
  primary consumer. Wind down when (and only when) the daily query
  pattern moves to Claude Desktop.

These two are NOT removed in this ADR. They're flagged for cuts in
follow-up PRs as the MCP path proves out. Wind-down sequence is
calibration-paced, not feature-paced.

**The architectural picture is three layers, not two:**

1. **Push artifacts (scheduled, keep)** — Morning Brief, Evening
   Reflection (both modes), Brag Spotter, Risk Watcher daily
   fan-out, daily cost card. These have value *because* they're
   scheduled — they impose cadence and produce dated artifacts.
2. **Signal generators (keep + extend)** — Triage, Risk Watcher,
   Notes / Calendar / CRM ingesters, Librarian, Captures Materializer,
   Reflection's voice-memo extraction. Produce structured rows in
   BQ, not prose. Always have value.
3. **Conversation / retrieval layer (move out via MCP)** — `brain_ask`,
   `client_summary`, `open_risk_flags`, `capture_note`,
   `mark_decision_status`. The on-demand surface. Replaces the
   Surfacer's synthesis step + Chat surface.

The conversation layer reads from layers 1 + 2; layers 1 + 2 do not
depend on the conversation layer.

### §4 — Calibration over construction

After PR #127 lands and the MCP server ships, the project enters a
**calibration phase**, not a build phase:

- Let Risk Watcher's longitudinal signal accumulate enough history
  to threshold-tune (current `risk_profiles` thresholds were picked
  before seeing real prod data).
- Let the corpus grow (H1 Solutions Drive sweep + H2 Gmail-into-corpus
  + Captures form) so retrieval has enough material that `notes_links`
  populates meaningfully.
- Operator unblockers from `docs/ROADMAP.md` §Next get done as
  scheduled, but no new feature work goes into the Brain unless it
  generates a new bespoke signal that off-the-shelf can't.

The default answer to "should the Brain do X" becomes "no — does
Claude with MCP do X with the inputs the Brain exposes?"

## Out of scope

- **The `mcp-server-brain` implementation itself.** Follow-up PR.
  Tool definitions in §2 are starting points, refined during build.
- **Voice infrastructure.** Lives in the Agent Coordination dashboard
  repo per ADR 0050 §Out of scope.
- **Specific narrative-composer deprecation PRs.** Each composer
  gets its own small PR (or none — if it's harmless and running,
  leave it alone).
- **Replacing the Brain with off-the-shelf entirely.** The signal
  layer is bespoke and stays. This ADR is about the *interface*
  layer, not the substrate.

## Consequences

- **Scope shrink.** The "what's left to build" list collapses. The
  remaining engineering is the MCP server (~1 day) + calibration +
  occasional deprecation PRs. The Brain is feature-complete for the
  substrate role.
- **Maintenance burden inverts.** Each narrative composer carried
  scheduler IAM, DWD impersonation, Gemini token cost, prompt
  versioning, and brittle Workspace Add-ons response wrappers
  (PR #117's IAM hotfix is what that burden looks like). Moving
  composition to the conversation layer drops most of that.
- **The Brain becomes swap-tolerant on its consumer side.** Today's
  consumer = the Workspace Add-ons Chat App + the AI Ops Dashboard
  (read-only + decisions writeback per PR #128). Tomorrow's
  consumer could be Claude Desktop, ChatGPT, Cowork, or local
  Llama — all via the same MCP seam.
- **Cost stays under the $50/mo guardrail** (ADR 0024). The MCP
  server adds zero infra cost (local-process stdio). Removing
  narrative composers reduces Vertex/Gemini token spend marginally.
- **No load-bearing invariants change.** ADRs 0013 (BQ US
  multi-region), 0017 (Model Armor disabled at runtime), 0024 (cost
  guardrails), 0027 (DWD allowlist), 0028 (no new Reasoning Engines),
  0037 (single agent_outputs dataset), 0038 (BQ VECTOR_SEARCH, no
  managed index), 0044 (Drive write via folder-share), 0045
  (Librarian multi-root), 0050 (`/api/ask`) all unchanged.

## Supersedence

- **Extends ADR 0050** (Brain API surface). `/api/ask` becomes one of
  ~10 MCP tools rather than the only programmatic surface.
- **Amends ADR 0046** (Knowledge Surfacer model + surface). The
  retrieval pipeline (embed → VECTOR_SEARCH → guardrail) stays as
  the `/api/ask` backend. The Chat surface and the
  `gemini-2.5-flash` synthesis step are flagged for deprecation
  (§3).
- **Does not supersede** ADR 0029 (Morning Brief), 0036/0040
  (Evening Reflection), or 0043 (Brag Spotter). Those composers
  keep running until specific cuts are decided per agent; this ADR
  reframes the future direction but leaves operational reality
  intact.
