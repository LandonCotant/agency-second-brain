# MCP server — `asb-mcp-server`

Local stdio MCP server that exposes the Brain's signal layer to any
MCP-speaking client (Claude Desktop, ChatGPT, local models via MCP
bridges, etc.). ADR 0051 §2.

## What it exposes

Fifteen tools — eight read, seven write — each with inline docstrings
the host LLM uses for tool selection:

| Tool | Direction | Purpose |
|---|---|---|
| `brain_ask` | read | Embed + VECTOR_SEARCH retrieval over `agent_outputs.notes`. Returns chunks; the host LLM synthesizes. |
| `open_risk_flags` | read | Active risk flags (`resolved_at IS NULL`), optionally filtered by segment + min severity. |
| `open_drafts` | read | Airtable Tasks with `approval_status='Drafted by Agent'` awaiting operator approval. "What's queued for me?" |
| `pending_followups` | read | Contacts whose `next_followup` is due today or earlier; optional `window_days` look-ahead. "Who am I behind on?" |
| `client_summary` | read | Cross-dataset JOIN — account + active project + 30d triaged_items + open risk_flags + recent meetings + recent emails. |
| `get_calendar_events` | read | Enumerate calendar events by date range. Use for time-bound queries that `brain_ask` under-weights (birthdays, deadlines). |
| `related_notes` | read | Walk `notes_links` for a given note (ADR 0053). Filter by `link_types=['wikilink']` or `['semantic']`. |
| `person_summary` | read | Structured briefing on a person across Airtable contacts + accounts + recent activity (ADR 0057 §6). Prefer over `brain_ask` for "who is X" / "brief me on X" of a specific human. |
| `capture_note` | write | Hash-keyed MERGE into `agent_outputs.notes` with `note_kind='capture'`, `scope='personal'`. Replaces the Airtable Captures form for ad-hoc mobile use. |
| `mark_decision_status` | write | Transition a decision from `drafted` → `confirmed`/`dismissed`. Guarded by `WHERE status='drafted'`. |
| `insert_decision` | write | Insert a new decision row, always with `status='drafted'`. Used by migrated Evening Reflection routines. |
| `insert_win` | write | Insert a win with the `title_hash12` dedup pattern from ADR 0043 §3. |
| `record_feedback` | write | Record an operator verdict on a signal into `agent_outputs.signal_feedback` (ADR 0060). A `noise` verdict mutes future emissions of that `(account, pattern)` flag; `mute_days` time-boxes it. Other verdicts (`valid`/`wrong_tone`/`wrong_target`) recorded for Stage-2 tuning. |
| `update_weekly_doc` | write | Prepend a markdown section to this week's Brief / Reflection / Review Doc in Drive (ADR 0044 + PR #147 + PR #151). |
| `sync_people` | write* | Fires `asb-people-sync` Cloud Run Job on demand to refresh `Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/` from Airtable (ADR 0057). Shells to `gcloud run jobs execute`. |

\* `sync_people` doesn't write data itself — it triggers the Cloud Run Job that does the writes.

## Install

From the repo root:

```bash
pip install -e ".[mcp-server]"
```

That pulls the `mcp>=1.2` SDK and registers the `asb-mcp-server`
console script.

## Auth

Plain `gcloud auth application-default login` is enough — the operator's
ADC has owner on the brain project, which covers BQ + Vertex (the
ten read tools + the four BQ-writing tools).

Drive/Docs (`update_weekly_doc`) needs a separate path because the
ADC OAuth client has a hardcoded scope allowlist that drops `drive`
silently at the consent step, so user-cred ADC physically can't get
a Drive token no matter what `--scopes` flag you pass. The MCP server
sidesteps this by impersonating `asb-agent-triage-sa` for Drive/Docs
calls only (`clients.py:_drive_scoped_creds`). The SA self-mints a
Drive-scoped token via `iamcredentials.generateAccessToken`, which
works because project owner implicitly grants
`iam.serviceAccounts.getAccessToken` on every SA in the project.

Per ADR 0044 the user must share the rollup folders with the SA email
as Editor. Evening Reflection already shares the Reflections folder
with `asb-agent-triage-sa`. The Briefs and Reviews folders need the
same one-time share (Drive UI → folder → Share → add
`asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com` →
Editor). After that, plain `application-default login` is fine to
run anytime — re-runs don't break the Drive path because Drive uses
the SA's scopes, not the user's.

Override the impersonated SA via `BRAIN_DRIVE_IMPERSONATION_SA` if
needed (e.g., a dedicated `asb-mcp-sa` later).

Per-tool safety wrappers (drafted-status checks, hash-keyed MERGEs,
scope/note_kind enforcement) are the primary defense against
prompt-injected misuse rather than IAM scoping.

## Claude Desktop configuration

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) and add:

```json
{
  "mcpServers": {
    "brain": {
      "command": "asb-mcp-server",
      "env": {
        "BRAIN_PROJECT_ID": "agency-brain-demo"
      }
    }
  }
}
```

Restart Claude Desktop. The seven tools should appear under the
`brain` MCP server entry. Test with: "Use brain_ask to find anything
about Client A."

## Smoke test (after Claude Desktop wiring)

Three quick checks:

1. **Read path:** ask Claude something that should hit the corpus
   (e.g. recent calendar events). Expect chunks back from `brain_ask`.
2. **Risk surface:** "What risk flags are open?" → `open_risk_flags`
   returns the current set (or empty list if none).
3. **Write path:** "Capture this note: smoke-test-2026-05-14" →
   `capture_note` returns a `note_id` and `inserted=true`. Re-run
   the same text — should return `duplicate=true` (the SHA-256
   dedup pre-check works).

## Configuration via env vars

All optional; defaults are sane for prod.

- `BRAIN_PROJECT_ID` — default `agency-brain-demo`
- `BRAIN_VERTEX_LOCATION` — default `us-central1`
- `BRAIN_NOTES_DATASET` / `BRAIN_NOTES_TABLE` — defaults
  `agent_outputs` / `notes`
- `BRAIN_OUTPUTS_DATASET` — default `agent_outputs`
- `BRAIN_MCP_COSINE_THRESHOLD` — default `0.50` (lower than Knowledge
  Surfacer's `/api/ask` because the host LLM does post-retrieval
  filtering during synthesis; recall over precision here)
- `BRAIN_MCP_TOP_K_DEFAULT` — default `8`

## Out of scope (use the dedicated connectors instead)

- **Arbitrary BQ queries** — install a generic BigQuery MCP server.
- **Airtable reads/writes outside the Brain's drafts-only path** —
  use Anthropic's Airtable MCP (`mcp__claude_ai_Airtable__*`).
- **Gmail send / draft / labels** — use Anthropic's Gmail MCP
  (`mcp__claude_ai_Gmail__*`).
- **Drive file create / read** — use Anthropic's Drive MCP
  (`mcp__claude_ai_Google_Drive__*`).

The Brain MCP server is deliberately narrow: it covers only the
agency-specific signals + writeback paths that the generic
connectors can't.

## Architecture references

- ADR 0050 — Brain API surface (`/api/ask`). The `brain_ask` tool
  shares the same retriever class (`knowledge_surfacer.retriever.Retriever`)
  but skips the synthesis step.
- ADR 0051 — Brain as MCP substrate. The defining architecture
  decision for this server.
- ADR 0046 — Knowledge Surfacer retrieval pipeline. The retriever
  implementation `brain_ask` reuses.
- ADR 0043 — Brag Spotter `title_hash12` dedup pattern. `insert_win`
  reuses this so MCP-inserted wins don't collide with the weekly
  Cloud Run job.
