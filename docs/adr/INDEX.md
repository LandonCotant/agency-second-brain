# ADR Index

68 ADRs on disk (`0001`–`0071`, with `0002` + `0062` skipped; `0066` not yet merged — arrives with the people-content-layer branch). Each ADR is the authoritative record of a decision; this file is a navigation layer with one-line taglines + supersedence chain. If you disagree with an ADR, write a new one that supersedes it — don't ignore it.

## Active ADRs (by area)

### Foundation & infra

- [0001](0001-cloud-build-over-github-actions.md) — Cloud Build over GitHub Actions for CI.
- [0003](0003-tfstate-bucket-bootstrap.md) — TF state bootstrap via one-shot script (`scripts/bootstrap_tfstate.sh`).
- [0004](0004-project-scope-org-policies-and-sinks.md) — Org policies at project scope (not org root) — 11 unrelated projects in the org block org-wide.
- [0005](0005-rely-on-default-cloud-audit-logs.md) — Default Cloud Audit Logs only; no custom retention sink (cost-driven).
- [0010](0010-cloud-run-job-for-airtable-sync.md) — Cloud Run Job (not Application Integration) for the Airtable sync; WRITE_TRUNCATE per cycle.
- [0012](0012-runtime-audit-architecture.md) — Runtime audit architecture (4 audit Cloud Run Jobs as a per-PRD-§4.1-layer system).
- [0013](0013-unify-bq-dataset-locations.md) — All BQ datasets at `US` multi-region (cross-dataset JOINs).
- [0014](0014-bump-google-providers-to-7x.md) — Bumped Google Terraform providers 5.30 → 7.30 for Model Armor + Reasoning Engine resources.
- [0018](0018-compute-default-sa-disabled.md) — Compute default SA disabled via `google_project_default_service_accounts`.
- [0024](0024-cost-guardrails.md) — Cost guardrails: AR cleanup, BQ partition TTLs, $50/mo budget alert.
- [0055](0055-hipaa-isolation-audit-dormant.md) — `asb-audit-sensitive-isolation` scheduler paused until HIPAA ingestion ships (audit was 100% false-positive — `airtable_replica.clients` doesn't exist; Operations base uses `accounts` per ADR 0020 and HIPAA ingestion is deferred). Complements ADR 0012; re-enable checklist embedded.
- [0058](0058-hipaa-iam-drift-audit-dormant.md) — `asb-audit-sensitive-iam-drift` scheduler paused. Misnamed (not actually HIPAA-scoped; it's a general brain-project IAM baseline drift check). Audit was functioning correctly, detecting legitimate drift from a baseline that hasn't been refreshed since post-WS-G1; user opted to quiet the HIPAA-named cluster while dormant rather than invest in baseline refresh. Extends ADR 0055 pattern; re-enable checklist embedded.

### Auth / DWD / IAM

- [0027](0027-dwd-delegation-surface.md) — DWD allowlist: one SA (`asb-agent-triage-sa`), one scope (`gmail.compose`); subject `owner@example.com`. Doc-driven audit posture.
- [0029](0029-morning-brief.md) — Morning Brief topology + DWD allowlist expansion to `{gmail.compose, calendar.readonly}` (one SA, two scopes).
- [0064](0064-dwd-impersonation-graph.md) — Addendum to 0027: documents the live impersonation graph (5 agent SAs + human + self-binding hold token-creator on `asb-agent-triage-sa`), accepts it as risk for the 2-person tool, retains the required self-token-creator binding, caps further expansion, and fixes two bindings to reference the SA resource instead of a hand-built path.
- [0065](0065-accept-cloudbuild-viewer-role.md) — Accept `roles/viewer` on `asb-cloud-build-sa` (read-only metadata, needed for `terraform plan` in PR checks) rather than narrowing to a custom role: low marginal risk vs real CI-breakage cost on an already-supply-chain-trusted build SA. IAM drift audit baselines the binding.

### Triage & routing

- [0006](0006-audit-log-streaming-and-emit-on-every-path.md) — Audit log streaming + emit-on-every-path (BaseAgent contract).
- [0008](0008-chat-alerting-via-google-chat-channel.md) — Native `google_chat` alert channel (NOT `webhook_tokenauth`; supersedes 0007).
- [0009](0009-agent-outputs-schema.md) — `agent_outputs.*` schema design (mixed canonicality: BQ-canonical for triage/risk, Airtable-canonical for goals).
- [0019](0019-triage-bridge-architecture.md) — Triage bridge: Cloud Run Job + every-5-min scheduler + Pub/Sub pull (DLQ at 5 attempts).
- [0022](0022-triage-no-match-policy.md) — Triage Agent no-match policy: drafts to sentinel "Triage Inbox", domain-match guard, free-mail skip-list. `Team.User` `_extract: user_id`.
- [0023](0023-routing-chat-fanout.md) — WS-D Chat fan-out (per-row dispatch via Google Chat webhook + Cloud Run Job + 5-min scheduler).
- [0025](0025-routed-events-table.md) — Dispatch tracking via insert-only `routed_events` table (replaces UPDATE-on-routed_to that hit BQ streaming-buffer DML restriction).
- [0026](0026-triage-dedup.md) — Triage dedup, three layers: bridge-side write removed, `input_hash` pre-INSERT dedup, exactly-once Pub/Sub delivery.
- [0032](0032-routing-gmail-draft-channel.md) — Routing Gmail draft channel + multi-channel fan-out via `RoutingFanoutAgent`. ADR 0027 §2 invariant preserved (`asb-routing-sa` impersonates the only DWD-grantable SA).
- [0060](0060-signal-feedback-and-suppression-loop.md) — **Proposed.** Signal feedback + suppression loop. New `agent_outputs.signal_feedback` table (keyed by `(account_id, pattern_name)` tuple, not flag instance — Risk Watcher re-mints `flag_id` per run, so instance-resolve doesn't stop recurrence) + conversational `record_feedback` MCP write tool. Stage 1: a `noise`-verdict suppression gate in `RiskFlagsWriter` that writes matching flags **pre-resolved** (`resolved_at` set) so both consumers (`open_risk_flags`, WS-D fan-out poll) hide them via their existing `resolved_at IS NULL` filter — zero consumer changes, full audit trail in `risk_flags`, `mute_until` time-box, never silent. Stage 2 (deferred): few-shot tuning of classification prompts + a learned tone guide. No new service/dataset; few-shot + SQL gate over managed ML. Extends 0034/0035, 0037, 0051.

### Model security

- [0015](0015-model-armor-via-templates.md) — Model Armor enforcement via Template resources, not RE config blocks. Item #3 superseded by 0017; items #1/#2/#4 still hold.
- [0017](0017-model-armor-disabled-at-runtime.md) — Model Armor disabled at runtime; `TB_ENABLE_MODEL_ARMOR=false` is permanent. Drafts-only carries the residual prompt-injection risk.

### Reasoning Engine posture

- [0016](0016-re-deploy-via-sdk-then-tf-import.md) — Reasoning Engine deploy via Python SDK then `terraform import` (TF resource unstable for create-from-scratch in 7.30).
- [0028](0028-reasoning-engine-create-alert.md) — RE creation alert as cost guardrail (driven by 2026-05-02 orphan-RE incident — $40/day burn from 3 orphan REs). Pages on every `CreateReasoningEngine` audit event.

### Airtable architecture

- [0020](0020-collapse-to-single-airtable-base.md) — Collapse to single Operations base (supersedes 0011). Operations.Accounts canonical; Contacts/Contracts moved in from CRM; Projects via real linked-record fields.
- [0063](0063-sync-preload-required-field-validation.md) — Pre-load required-field validation in `asb-airtable-sync` (`find_invalid_rows`). One half-entered record no longer blackholes a table's WRITE_TRUNCATE load: valid rows load, each incomplete row emits a precise `log.error` (table + record + all missing fields) that trips `asb-cloud-run-job-error` until fixed. Validate-and-report, NOT relax-to-NULLABLE (REQUIRED fields stay load-bearing) and NOT a sync-ready view filter (would trade a loud failure for a silent one). Execution exits 0 while the alert reddens — deliberate; the log is the right surface for a data problem. Sixth incident of this class. Extends 0010.

### Agent topology

- [0029](0029-morning-brief.md) — Morning Brief topology: Cloud Run Job + 7:25am-PT scheduler + Vertex SDK direct (NOT Reasoning Engine). "Always draft, even on quiet days."
- [0031](0031-samsung-notes-ingestor.md) — Notes ingestor: Drive folder polling (NOT DWD), Vertex Gemini multimodal, HIPAA via folder convention, revision-keyed dedup, daily cadence. Closeout addendum: PDF mimeType filter widened.
- [0033](0033-risk-watcher-topology.md) — Risk Watcher topology + signal-as-data architecture. Daily Cloud Run Job at 06:00 PT (NOT a RE — same posture as 0028/0029 §3). Memory Bank baselines, same-day dedup.
- [0034](0034-risk-watcher-followup-profiles.md) — Risk Watcher follow-up profiles: one Job multi-segment, parametrized `AcknowledgmentGapSignal`, loader package split, LS profile + AP skeleton. §5 OwnerDisengagement spec superseded by 0035.
- [0035](0035-owner-disengagement-multi-source.md) — Owner Disengagement v2: multi-source MAX (calendar exact-email + triage inbound + approved/done tasks). Loader gates on active contract + ≥1 CRM contact. Routing fan-out adds `WHERE rf.resolved_at IS NULL`.
- [0056](0056-retire-cloud-run-prompt-schedulers.md) — Retire Cloud Run `asb-morning-brief-daily` + `asb-evening-prompt-daily` schedulers (Gmail-draft surface) in favor of Local Claude Code routines that write to consolidated Drive Docs via `update_weekly_doc`. Jobs/SAs/IAM stay deployed for one-line revert. REFLECT 9 PM scheduler untouched. Same shape as ADR 0055.
- [0057](0057-personal-crm-airtable-bridge.md) — Personal CRM: Airtable → Brain Galaxy bridge. New `asb-people-sync` Cloud Run Job (weekly Sunday 06:15 UTC + manual via `sync_people` MCP tool) materializes `airtable_replica.{accounts,contacts}` as `.md` files under `Brain/05_GALAXY/{clients,people}/` with YAML frontmatter (warmth, relationship_type, last_contact, next_followup, etc.). Librarian's Galaxy sweep indexes them → wikilinks like `[[Client A]]` resolve into `notes_links` graph edges (ADR 0053). HIPAA cascade excluded. Additive-only deletion (`status: archived`). Body skeleton with `<!-- AUTO -->` sections for Active engagements / Recent activity / Open risks / Conversation log. New MCP tools `person_summary` + `sync_people`. Extends 0042, 0044, 0052, 0053, 0054 §2.

### PKM merge (Phases 0a–4)

- [0037](0037-pkm-merge-architecture.md) — PKM merge architecture: single `agent_outputs.*` dataset with `scope` column. IPARAG-adapted Drive layout, kind-gated triage publish, Galaxy-as-flag. No new DWD scopes.
- [0038](0038-embeddings-and-vector-search.md) — Embeddings + BQ `VECTOR_SEARCH`. `text-embedding-005` (768-dim) on every `agent_outputs.notes` row at write time. NO managed Vertex Vector Search index ($30+/mo idle).
- [0039](0039-pkm-phase-0b-captures-materializer-and-backfill.md) — Phase 0b: Captures materializer (`*/15 * * * *`) reads Airtable replica, dispatches by Kind, then DELETEs row. Embeddings backfill env-var-gated branch in notes-ingestor.
- [0040](0040-evening-reflection-v2-two-mode.md) — Phase 1: Evening Reflection v2 (supersedes 0036). Two modes: PROMPT (4pm anchor, Gmail draft) + REFLECT (9pm voice-memo extraction → Doc + decisions/wins INSERTs).
- [0041](0041-decisions-reviewer.md) — Phase 2: Decisions Reviewer. NO new Job — routing fan-out gains a third polling source for `decisions WHERE status='draft'`. Synthesizes `severity='high'` so existing Chat window batches a daily-digest UX.
- [0042](0042-pkm-phase-4-personal-crm-and-risk-watcher.md) — Phase 4: Personal CRM + Risk Watcher 4th segment. New Contacts fields (Warmth, Last Contact, Next Followup, Relationship Type) + `PersonalReEngagementSignal`. Slots into existing daily 06:00 PT tick.
- [0043](0043-brag-spotter.md) — Phase 3: Brag Spotter. Sunday weekly Cloud Run Job sweeps last 7d via Gemini + `response_schema`. Always-fires; `title_hash12` cross-agent dedup.

### Drive write architecture

- [0044](0044-drive-write-via-folder-share.md) — Drive write via folder share + ADC, NOT DWD impersonation (preserves 0027 §2 invariant). HIPAA defense in depth: env-var allowlist + `_FORBIDDEN_FOLDER_ROLES` + SA never shared on HIPAA folder.
- [0045](0045-librarian-as-ingestor-and-multi-root.md) — Librarian-as-ingestor + multi-root + cross-Drive realities (extends 0044). Solutions Shared Drive as 2nd root; corpus write at classify time; copy-fallback on cross-Shared-Drive 403; archive-on-copy + audit skip-list; anonymous-filename rename; VECTOR_SEARCH dimension pre-filter.
- [0071](0071-notes-merge-on-note-id.md) — **Accepted.** Librarian notes write MERGEs on `note_id` (== Drive `file_id`), replacing INSERT-append + `(file_id, revision_id)` dedup for the galaxy / area / resource kinds. Fixes weekly-sweep row accumulation (asb-people-sync rewrites galaxy dossiers in place → new `headRevisionId` → the old dedup missed → 6 rows for one "Client A.md"). Idempotency keys on `note_id`: unchanged revision skips embed+MERGE; changed revision UPDATEs the single row in place. `triaged_item_id` left untouched on UPDATE. Streaming-buffer-safe (sweeps are days apart; calendar_event already MERGEs this table). One-time backfill prune keeps newest row per `(note_id, note_kind)`. Notes Ingestor inbox flow untouched. Retriever `QUALIFY ROW_NUMBER` dedup retained as defense-in-depth. Amends 0045 / 0054 §2; aligns with 0046; extends 0025, 0057, 0068.

### Knowledge Surfacer (WS-G7)

- [0046](0046-knowledge-surfacer-model-and-surface.md) — WS-G7 Knowledge Surfacer: `gemini-2.5-flash` (Pro escalation on low confidence), BQ `VECTOR_SEARCH` retrieval, Google Chat slash-command surface (first Cloud Run *service* in the codebase), app-layer entity-presence guardrail. Supersedes PRD §3 row + §6.6 (Sonnet/Knowledge Catalog) + §4.4 (Surfacer Model Armor). **Service + Chat surface retired by 0059; the `Retriever` library survives for `brain_ask`.**
- [0050](0050-brain-api-surface.md) — Brain API surface: second Flask route `POST /api/ask` on the Surfacer service for the user's separate Agent Coordination dashboard's Gemini Live tool calls. JSON in/out (no Chat card wrapping). New caller SA `asb-brain-api-caller-sa`; dual-caller OIDC verifier. Opt-in via `brain_api_enabled` TF flag. **Retired in full by 0059 (dashboard permanently deferred).**
- [0059](0059-retire-knowledge-surfacer-service-and-evening-reflection.md) — Claude app is the canonical interface; retire the GCP-side interactive surfaces. Deletes the `asb-knowledge-surfacer` Cloud Run service + SA + role + `/api/ask` (supersedes 0046 §3 + 0050 in full; keeps the `Retriever`/`models` library for `brain_ask`). Pauses `asb-evening-reflection-daily` REFLECT scheduler (Claude workflow replaces it; Job kept for revert — mirrors 0056). Dashboard permanently deferred. Completes the cleanup ADR 0051 §3 started. Apply-before-merge ordering load-bearing (sa-allowlist gate scans live SAs).
- [0051](0051-brain-as-mcp-substrate.md) — Brain identity reframe: signal substrate + writeback orchestrator, NOT a chat product. Expose capabilities via a new `mcp-server-brain` (local stdio, ~5 tools v0.1) so any MCP-speaking client (Claude Desktop, ChatGPT, local models, the dashboard's Gemini Live) does on-demand conversation. **Amended 2026-05-14:** deprecation list narrowed to two items only (Surfacer's `gemini-2.5-flash` synthesis step + Workspace Add-ons Chat surface). Morning Brief, Evening Reflection (both modes), and Brag Spotter STAY — they're scheduled push artifacts + structured-signal generators, not request-response narrative composers. Three-layer architecture: push artifacts + signal generators (keep) → MCP retrieval layer (new). Extends 0050; amends 0046.
- [0052](0052-synthetic-notes-for-decisions-wins.md) — Synthetic note rows for decisions + wins findability via `brain_ask`. Each write to `agent_outputs.decisions` / `agent_outputs.wins` also writes a companion row to `agent_outputs.notes` with `note_kind` ∈ {'decision', 'win'} + embedding, so the existing VECTOR_SEARCH path retrieves them without UNION-across-tables SQL changes. Retriever's `INCLUDED_NOTE_KINDS` additively widened to also include 'capture' (closes a latent bug where `capture_note` writes were invisible to `brain_ask`). Purely additive — canonical tables unchanged. Extends 0046, 0051.
- [0053](0053-wikilinks-and-related-notes.md) — Explicit `[[X]]` wikilink edges + `related_notes` MCP tool. Adds `link_type` NULLABLE column to `notes_links`; existing semantic rows interpreted as NULL ↔ `'semantic'`. Parser hooked into `capture_note` (and `_insert_synthetic_note` per ADR 0052). `related_notes(note_id, depth=1, link_types=None)` walks the graph. Additive only — Librarian's semantic-neighbor writer untouched. Extends 0045, 0052.
- [0067](0067-remote-mcp-on-cloudflare-workers.md) — **Proposed.** Remote brain MCP on Cloudflare Workers: TypeScript port of all 16 stdio tools behind `workers-oauth-provider` (OAuth 2.1 + DCR for claude.ai custom connectors), Google Workspace-internal upstream IdP + server-side email allowlist, `McpAgent` on a SQLite Durable Object + KV. **Keyless GCP auth via Workload Identity Federation** (project org policy blocks SA keys): the Worker self-hosts an OIDC issuer (JWKS) → WIF pool → impersonates `asb-mcp-sa` (and `asb-mcp-sa`→`asb-agent-triage-sa` for Drive); the only secret is the Worker's own RSA signing key, rotatable via the JWKS. Blast radius bounded by drafts-only. `brain_ask` dedups chunks by `note_id`. Local stdio server retained as fallback until cutover. Supersedes 0051 §2 (transport/auth) for the remote surface; amends 0064 (impersonation graph +1). $0/mo (free tiers).
- [0068](0068-hybrid-retrieval-vector-keyword-rrf.md) — **Accepted.** Hybrid retrieval for `brain_ask`: keyword arm (BQ `SEARCH()` over `markdown_content`+`filename`, scored by matching-term count via per-term scalar params since `SEARCH()`'s 2nd arg must be constant) fused with the existing VECTOR_SEARCH arm via Reciprocal Rank Fusion (`1/(k+rank)`, k=60); recency is a **third RRF channel**, not a multiplier (multiplier let recency dominate the compressed RRF range — caught + fixed by the eval). All in one SQL query → ports verbatim to the 0067 Worker. Four invariants held in both arms. `notes_keyword_idx` search index created via DDL (`scripts/create_search_index.py`) — no native TF resource (#12388). `BRAIN_HYBRID_ENABLED` flag (default on); keyword-only degrade on embed outage; `match_type`/`rrf_score` on responses. Golden-query harness (`scripts/eval/`) is the regression guard: sparse/exact-match MRR 0.67 → 1.00. Additive to 0038 (keyword index ≠ managed vector index, $0 idle). Graph channel + IDF term-weighting deferred. Extends 0038, 0046, 0051, 0067.
- [0069](0069-action-memory-commitment-extraction.md) — **Proposed.** Action memory (Phase 2): a daily `asb-commitment-extractor` Job mines commitments ("who promised what, by when", `direction: mine|theirs`) from `agent_outputs.notes` into a new `agent_outputs.commitments` table — one post-processor over the corpus, not four agent changes (all sources already land in `notes`). Gemini 2.5 Flash structured extraction (per-note, `thinking_budget=0`, confidence floor); `account_id` resolved from counterparty email; `agent_state` watermark + `NOT IN commitments` dedup. Overdue = `COALESCE(due_date, extracted_at + COMMITMENT_STALE_DAYS) < today`. Surface is the `open_commitments` MCP tool (the paused Morning Brief agent is NOT wired — Local routine calls the tool, ADR 0056). Drafts-only; new SA + custom role `tbCommitmentExtractor` (no `agent_audit_log` grant — Cloud Logging observability). Extends 0037, 0049, 0046, 0051, 0057.
- [0070](0070-bitemporal-factual-memory.md) — **Proposed.** Bi-temporal factual memory (Phase 3): a daily `asb-fact-extractor` Job mines entity-attribute facts (`entity · predicate · value`, e.g. "Acme · retainer · $3k") from `agent_outputs.notes` into the **append-only** `agent_outputs.facts` log. Bi-temporal: `observed_date` (event time) + `extracted_at` (transaction time); current-vs-superseded derived at **read time** (latest `observed_date` per `(entity, predicate)` wins; `valid_to` via `LEAD`) — no UPDATE, sidesteps streaming-buffer DML (ADR 0025). Deterministic same-key supersession (LLM extracts, never judges contradiction). Gemini 2.5 Flash, confidence floor 0.7 (higher than commitments — facts read as truth); entity resolved to accounts/contacts. Surface = `entity_facts` MCP tool (17th); `client_summary`/`person_summary` untouched in v1. Drafts-only/advisory; new SA + custom role `tbFactExtractor`. Completes the three Sentra memory layers. Extends 0069, 0037, 0051, 0025, 0057.
- [0054](0054-librarian-resources-and-galaxy.md) — Librarian: Resources bucket + Galaxy as capture surface. `AreaFolder.bucket ∈ {areas, resources}` carries bucket out-of-band on classifier candidates (no LLM schema change). `LIBRARIAN_DEST_ROOTS` env grammar widens to `bucket=label:folder_id` (2-segment legacy still works). `note_kind=resource` retriever widening — `INCLUDED_NOTE_KINDS` additively includes `'resource'`; `'archive'` still excluded. §2 (Galaxy as drop-to-index folder) supersedes ADR 0037 §2's sub-decision rejecting a Galaxy folder; Galaxy-as-flag invariant preserved. Archives lifecycle deferred (design gap: `airtable_replica.projects` lacks per-project Drive folder URL). Extends 0037, 0045.

### CRM Auto-updater

- [0047](0047-crm-auto-updater-and-gmail-readonly-scope.md) — CRM Auto-updater + DWD scope expansion to `{gmail.readonly, gmail.modify}` on `asb-agent-triage-sa`. Reads `secondbrain`-labeled emails, drafts Airtable Tasks + Contacts/Accounts `Pending Updates` blocks. Drafts-only per PRD §4.7; `asb-crm-updater-sa` impersonates triage SA. Supersedes ADR 0027 §2 (DWD scope list).
- [0049](0049-gmail-into-corpus.md) — Gmail-into-corpus side effect inside the CRM Auto-updater. Per-email `text-embedding-005` + INSERT into `agent_outputs.notes` with `note_kind='email'`, `scope='agency'`. Idempotent on `(external_id, note_kind='email')`. No new IAM / DWD / Dockerfile. Closes the WS-G7 v2 "Gmail-into-corpus" question.

### Solutions Drive ingestion (Phase H — WS-G corpus expansion)

- [0048](0048-solutions-drive-ingestion.md) — Notes Ingestor widened to walk the the agency Shared Drive: per-client subfolder allowlist + HIPAA-by-account-name filter, recursive sweep of the 4 internal top-level folders, `Client:` / `Source:` markdown header prepended pre-embed for semantic retrieval. Extends ADR 0031 / 0037. No new DWD scope.

### Cost

- [0024](0024-cost-guardrails.md) — Cost guardrails: AR cleanup, BQ partition TTLs, $50/mo budget alert.
- [0028](0028-reasoning-engine-create-alert.md) — RE creation alert.
- [0030](0030-daily-spend-check.md) — Daily cost tripwire. 5th audit Job queries BQ billing export for previous-24h spend per project; threshold breach emits `COST_THRESHOLD_EXCEEDED`. Manual kill switch at `scripts/disable_billing.sh`.

## Superseded / historical

- [0007](0007-chat-alerting-via-space-webhook.md) — Webhook-tokenauth alert channel — superseded by 0008 (didn't work in practice).
- [0011](0011-airtable-operations-base.md) — Two-base Airtable architecture — superseded by 0020 on 2026-04-30.
- [0015](0015-model-armor-via-templates.md) item #3 — Runtime enforcement — superseded by 0017.
- [0021](0021-crm-minimal-read-sync.md) — Minimal CRM read-sync — never implemented; superseded by 0020 before landing. Kept as historical record.
- [0034](0034-risk-watcher-followup-profiles.md) §5 — OwnerDisengagement v1 spec — superseded by 0035.
- [0036](0036-evening-reflection.md) — Evening Reflection v1 — superseded by 0040 (PKM Phase 1).
- [0050](0050-brain-api-surface.md) — Brain API surface `/api/ask` — retired in full by 0059 (dashboard permanently deferred).
- [0046](0046-knowledge-surfacer-model-and-surface.md) §3 — Knowledge Surfacer Cloud Run service + Chat `/ask` surface — retired by 0059 (zero usage; `brain_ask` is the canonical surface). Retriever library survives.

## Supersedence chain

- 0007 → 0008
- 0011 → 0020
- 0015 §3 → 0017
- 0021 → 0020 (never landed)
- 0034 §5 → 0035
- 0036 → 0040
- PRD §3 row "Knowledge Surfacer LLM" → 0046
- PRD §6.6 (model + retrieval) → 0046
- PRD §4.4 (Surfacer Model Armor "required") → 0046
- ADR 0027 §2 (DWD scope list `{gmail.compose, calendar.readonly}`) → 0047 expands to `{gmail.compose, calendar.readonly, gmail.readonly, gmail.modify}`
- ADR 0037 §2 sub-decision (Galaxy as flag only, no folder) → 0054 §2 (Galaxy as drop-to-index folder + flag; the move-on-promote rejection in 0037 §2 is supersedeed by the drop-to-index model)
- ADR 0029 + ADR 0040 §1 scheduler/surface decisions → 0056 (scheduler retired in favor of Local Claude Code routines; Jobs/SAs/IAM kept for revert; 0040 §2 REFLECT untouched)
- ADR 0046 §3 (Knowledge Surfacer service + Chat surface) + ADR 0050 (`/api/ask`) + ADR 0040 §2 (REFLECT scheduler) → 0059 (Claude app canonical; service deleted, retriever library kept, REFLECT scheduler paused, dashboard permanently deferred; completes ADR 0051 §3)
- ADR 0051 §2 (local stdio transport + operator ADC, no new SA) → 0067 (remote surface on Cloudflare Workers with OAuth 2.1 + `asb-mcp-sa`; local stdio server retained as fallback until parity)
- ADR 0045 / 0054 §2 (Librarian notes INSERT-append + `(file_id, revision_id)` dedup) → 0071 (MERGE-on-`note_id` for galaxy/area/resource; one current row per note; backfill prune)
