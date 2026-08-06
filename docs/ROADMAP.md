# Roadmap

Current focus + queued work. For history, see `git log` and PR descriptions. For deployed state, see `docs/PRODUCTION_STATE.md`.

## Architecture (the shape we're building toward)

Three layers, converged through ADRs 0050-0058:

1. **Agents = signal generators.** Cloud Run Jobs (Triage, Risk Watcher, People Sync, Librarian, Notes Ingestor, Captures Materializer, Brag Spotter, Evening Reflection, Routing Fanout, audit jobs) produce structured outputs against schemas. They run unattended. The LLM calls they make are deterministic classification/extraction, not free-form prose — keeps them vendor-portable (swap Gemini → Claude Haiku → any-model with one config change).
2. **Frontier LLM = composer.** Claude (today via Claude Desktop / Claude Code; tomorrow potentially any MCP-speaking client) consumes the agents' signals via MCP tools and synthesizes free-form output — Morning Brief, Evening Reflection prompts, replies, captures, weekly reviews. ADR 0056 codified this by retiring the Cloud Run Morning Brief scheduler.
3. **MCP server = open-protocol interface.** `asb-mcp-server` (stdio subprocess on operator's Mac) exposes 14 tools today. ADR 0051 named the pattern. PR #164 introduced shared SQL primitives (`common/bq_helpers.py`) so per-tool maintenance cost stays bounded as the count grows.

**Forward thread:** keep wiring the knowledge layer (Galaxy, brain_ask, person_summary, related_notes) into the agency-ops layer (Triage, Risk Watcher, Morning Brief) so agency outputs get smarter without each agent re-deriving context.

**Portability constraint:** no layer locks the project to one vendor — BQ is GCP but schema-portable; agent LLM calls are SDK-thin and model-swappable; MCP is open protocol; operator-side synthesis happens at whatever frontier LLM the operator points their MCP client at.

## Now (in-flight)

_(Production-readiness audit completed 2026-05-28 — `docs/audit/2026-05-28-production-readiness.md`. Verdict: GREEN. 10 Tier-2 findings carried out for triage; the two with deadlines are F2 (stranded Cloud SQL costing $9.60/mo) and F4 (Vertex `vertexai` SDK EOL 2026-06-24, 27 days out). Audit branch `audit/production-readiness-2026-05-28` adds `scripts/check_airtable_schema_drift.py` + 41 new unit tests across audit modules and the drift script — see PR.)_

**F4 fully retired 2026-05-29.** Investigation of the official deprecation notice showed the 2026-06-24 EOL is scoped to the *Generative AI module only* (`generative_models`/`language_models`/etc.) — **not** `vertexai.agent_engines`, so the triage Reasoning Engine (the only `vertexai` user on an enabled scheduler) is out of scope and needs no migration; the Pattern D-1/D-2 question is retired. Phase 2 shipped the last two GenAI-module files (`morning_brief/main.py` + `evening_reflection/main.py` PROMPT path) to `google.genai`. Zero deprecated-GenAI-module references remain in `src/`. See closeout addendum in `docs/audit/2026-05-28-vertex-sdk-migration-plan.md`.

**Consolidation toward the Claude app as canonical interface — ADR 0059 (2026-05-31).** Completes the cleanup ADR 0051 §3 started. The `asb-knowledge-surfacer` Cloud Run service + `/ask` Chat surface + `/api/ask` route (ADR 0050) are retired (zero usage; `brain_ask` MCP tool is the canonical query surface; retriever library kept). `asb-evening-reflection-daily` REFLECT scheduler paused (replaced by a scheduled Claude workflow; Job kept for revert — mirrors ADR 0056). **The dashboard is permanently deferred** (see v2 questions). Pending operator-approved targeted `terraform destroy` (apply-before-merge: the sa-allowlist gate scans live SAs).

## Next (committed, sequenced)

Phase H unblocking completed 2026-05-25: Chat App registered, DWD scopes expanded, Solutions Drive shared with ingestor SA, Solutions folder IDs populated (4 of 5; Finance & Accounting excluded — sensitive data).

The MCP audit (2026-05-19) produced a 3-step sequence. Step 1 shipped (PR #164 — shared `bq_helpers`).

- ~~**Audit step 2 — `open_drafts` + `pending_followups` MCP tools.**~~ Shipped with PR #164.
- ~~**Audit step 3 — test the "no composite" hypothesis.**~~ **Validated 2026-05-25.** Claude called all 4 primitives (`get_calendar_events` + `open_risk_flags` + `open_drafts` + `pending_followups`), cross-referenced across them (risk flags + overdue followups + pending drafts on the same accounts), and produced an actionable synthesis. Composite tools (`daily_picture` / `account_review` / `weekly_situation`) are unnecessary — skip them.
- ~~**Wire Brief routine → `person_summary`.**~~ **Done 2026-05-25.** Updated local Claude Code routine (`~/.claude/scheduled-tasks/morning-brief--daily/SKILL.md`) to call `person_summary`, `open_drafts`, and `pending_followups`. Validated: enrichment weaves warmth/last_contact/phone inline, cross-references risk flags with overdue followups and pending drafts.
- ~~**Wire Triage Agent → Galaxy.**~~ **Done 2026-05-25.** New `SenderContactLoader` injects sender warmth/relationship/account context into the classification prompt. Severity mapping updated to use warmth as a tiebreaker. Backwards-compatible (sender_context defaults to None). Requires triage-bridge image rebuild to deploy.
- ~~**`triaged_items.account_id` backfill.**~~ **Done 2026-05-25.** Added `account_id` column to BQ schema, threaded through Triage Agent write path (from `ProjectResolver.account_id` which was already captured but unused), updated `client_summary` MCP tool to JOIN on `account_id` instead of fuzzy name matching. Backfill script at `scripts/backfill_triaged_items_account_id.sql`. Deploy requires terraform apply + triage-bridge image rebuild.

## Tier 2 — housekeeping (anytime)

- ~~**Drop the vestigial `triaged_items.routed_to` column.**~~ Already removed from schema; no code references remain. Nothing to do.
- **Apply `bq_helpers` to `agents/people_sync/bq_enricher.py`.** 6 raw HIPAA/recency filter patterns identified. Defer until next enricher change.
- ~~**Verify + strike stale manual setup items.**~~ All resolved 2026-05-25: Chat App registered, Captures form deferred, e-commerce validation deferred.
- **Re-enable paused schedulers selectively.** All 3 pauses justified as of 2026-05-26. Review mid-June 2026:
  - `asb-audit-sensitive-iso` (ADR 0055) — blocked on HIPAA ingestion shipping
  - `asb-audit-sensitive-iam` (ADR 0058) — baseline stale from recent ADRs; ~30 min to refresh when ready
  - `asb-morning-brief-daily` (ADR 0056) — replaced by Local Claude Code routine; leave paused unless routine proves unreliable
- **Phase F — Morning Brief Areas-revisit.** Optional; raise after `notes_links` accumulates real neighbor sets over ~a week of usage.
- **Audit the 14 MCP tools for usage.** Review mid-June 2026 (~6 weeks post-ship). Too early now — server shipped 2026-05-14.

## v2 questions

- **Sibling `Pending Contact Updates` table for v1.5.** Promote the long-text staging field on Contacts/Accounts to a sibling table with explicit `Pending` / `Approved` / `Rejected` status if review proves ergonomically slow (PR B1 §"open question" recommended staying with long-text for v1).
- ~~**Dashboard for at-a-glance state.**~~ **PERMANENTLY DEFERRED — ADR 0059 (2026-05-31).** The Claude app (MCP read/write tools + scheduled Claude workflows) is the canonical operator interface; a separate dashboard would duplicate it. This closed `/api/ask` (ADR 0050), which existed only to feed the dashboard. Reopening requires a new ADR. (Original planning material in untracked `Solo AI Ops Dashboard Research.md`.)

## Deferred / parking lot

- **Populate Airtable Captures form.** Captures table exists but form is empty; `asb-captures-materializer` is a no-op until populated. Deferred indefinitely 2026-05-25.
- **Validate WS-G2 e-commerce against real signals.** Need an active E-commerce account in Operations.Accounts to exercise Triage + Risk Watcher `Acknowledgment Gap`. Deferred indefinitely 2026-05-25.
- **WS-G2d cross-cutting Acknowledgment Gap** (ADR 0034 §5) + **Klaviyo List Decline + ROAS Trend Down signals** — both gated on Vantage federation + ≥8 weeks of KPI history for baseline diffing.
- **Lift WS-G2c Agency Partner from skeleton.** Blocked on WS-B PR-4 (Vantage/Shopify federation tables); when those land, fill in the inline-commented queries in `loaders/agency_partner.py`.
- **Calendar attendees population.** Empty `attendees[]` arrays on operator's events mean Conversation log on Contact Galaxy pages can't pick up meeting-based interactions. Defer until calendar ingestion gets a refresh; until then the section keeps showing "no conversation traces."
- **Bidirectional Airtable sync** (ADR 0057 §9). Adds conflict-resolution complexity not worth it for v1; Brain stays read-only from Airtable's perspective.
- **Per-person standalone subfolders** (ADR 0057 §9). Flat `.md` files are fine for v1.
- **Met / Met Context fields on Contacts** (ADR 0057 §9). "How I know them" prose section captures the same intent; promote to structured columns later if needed.
- **Phase 5 — Connector / semantic linking** (subsumed). Librarian-as-ingestor writes `notes_links` per-file at classification time per ADR 0045; the dedicated Connector job is redundant.
- **Model Armor template removal** (ADR 0017 amendment, 2026-05-14): defer indefinitely. Template stays as a no-op in Terraform. Cost is zero; removing buys little.
