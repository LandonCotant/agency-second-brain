# Agency Second Brain — Implementation PRD

**Audience:** Claude Code (and the human reviewing what Claude Code produces)
**Source documents:** `proactive_agency_brain_spec.md` (the design), `agency_goal_hierarchy_v0.1.md` (the strategic context)
**Target start:** Week 1 of the 12-week build
**Owner of this PRD:** the operator
**Version:** 1.2

---

## 0. How to Use This Document

This PRD translates the design spec into a build plan structured for AI coding agents. It does three things the spec does not:

1. **Decomposes the work into independent workstreams** that multiple Claude Code instances can execute in parallel.
2. **Specifies a concrete repository structure, naming conventions, and tech-stack decisions** so Claude Code can generate code without re-deciding architecture.
3. **Treats security as a first-class concern** with explicit least-privilege requirements at every layer.

When a section here disagrees with the spec, this PRD wins for implementation purposes. When this PRD is silent on a behavioral detail, defer to the spec.

**Read order for Claude Code:** §1 → §2 → §3 → §4 (security charter) → §5 (workstreams) → relevant per-component sections (§6–§11) on demand.

**Platform note:** As of April 2026, Vertex AI is rebranded as **Gemini Enterprise Agent Platform**. The GCP service name (`aiplatform.googleapis.com`), SDK packages (`google-cloud-aiplatform`, `vertexai`), and Terraform resource types are unchanged — the rebrand is a product layer over the same APIs. This PRD uses Agent Platform terminology throughout. Where it matters for Claude Code, the underlying API resource is named in parentheses (e.g., "Reasoning Engine" is the API resource for a deployed agent).

---

## 1. Build Goals

In priority order:

1. **Ship Brain v1 in 12 weeks** with both partners using it daily by week 5.
2. **Zero HIPAA isolation breaches** detectable in any audit pass during or after the build.
3. **Least privilege everywhere.** No service account, IAM grant, or OAuth scope that exceeds what is strictly required for its job.
4. **Maximize parallel execution across Claude Code instances.** Workstreams are designed for clean handoffs and minimal cross-branch merge conflicts.
5. **Every agent action is auditable.** Inputs, outputs, confidence scores, and timestamps land in a queryable table within seconds of the action.

## 2. Non-Goals

The following are explicitly **out of scope for v1** and any work toward them is rejected during code review:

- Auto-sending any email or message on behalf of a user (drafts only — see §4.6)
- Writing to Vantage data, the HIPAA project, or any client-owned system
- Multi-tenant isolation beyond the single Agency workspace (no white-label yet)
- Custom UI beyond what's already provided by Gmail/Chat/Airtable/Gemini Enterprise
- Voice interfaces, predictive scoping, client-facing variants (all v2+)
- Goal Steward intake for team members beyond the operator and partner
- Frontier ML model training — this is an agent orchestration build, not a research project
- **RAG Engine adoption for the Knowledge Surfacer** — v1 stays with Knowledge Catalog as the unified semantic layer; RAG Engine is a v2 evaluation (see Appendix C.7)
- **Agent Studio (low-code) for any agent** — everything is built in ADK for codebase consistency

## 3. Tech Stack & Naming Decisions

These are decided. Do not re-litigate during implementation.

| Layer | Choice | Rationale |
|---|---|---|
| Cloud | Google Cloud Platform | Spec mandate; Workspace integration |
| Project layout | `agency-brain-demo` (this build), `agency-vantage-*` (existing, untouched), `agency-hipaa-*` (existing, never touched) | HIPAA isolation at project boundary; single prod project (no separate dev) |
| Primary language | Python 3.12 | ADK, GCP SDKs, BigQuery client all first-class |
| Agent framework | **Gemini Enterprise Agent Platform — Agent Development Kit (ADK)** | Per spec; deploys to Reasoning Engines |
| Agent hosting | **Agent Runtime** (Reasoning Engines API: `projects.locations.reasoningEngines`) | Sub-second cold starts; supports multi-day long-running agents needed by Risk Watcher and Goal Steward |
| Agent versioning | `runtimeRevisions` on each Reasoning Engine | Promotes prompts and code without redeploy churn |
| Agent state | **Agent Memory Bank** for per-client baselines and per-user goal context | Resolves spec §14 open question; first-class persistent context |
| Context optimization | **Cached Contents** for stable per-invocation context (active goals, client baselines) | Cost control; see §9 |
| Cron triggers | **Agent Platform Schedules** (`projects.locations.schedules`) for agent invocations; Cloud Scheduler for non-agent sync flows | Fewer moving parts than wiring Cloud Scheduler to every agent |
| Orchestration | Application Integration for sync + routing flows; Pub/Sub for the `triage-input` topic | Per spec |
| Default LLM | **Gemini 3.1 Pro** for agents; **Gemini 3.1 Flash** for high-volume Triage classification | Current generation as of April 2026 |
| Knowledge Surfacer LLM | **Claude Sonnet** via Model Garden | Conservative refusal posture fits the agent's high-precision requirement; see Appendix C.8 |
| Warehouse | BigQuery (single dataset per concern: `airtable_replica`, `agent_outputs`, `vantage_kpi_snapshots`, `agent_audit_log`) | Per spec |
| Semantic layer | Knowledge Catalog | Per spec; v1 retains for governance + retrieval (see §2 non-goals) |
| Secret storage | Secret Manager (no env vars, no `.env` files, no secrets in code) | Non-negotiable |
| Identity | **Agent Identity** (built into Agent Platform) for agent SAs; Workload Identity Federation for any non-GCP runtime | No long-lived keys; cryptographic agent IDs from the platform |
| Prompt-injection defense | **Model Armor** (`ModelArmorConfig`) on agents that ingest external content | Triage Agent and Knowledge Surfacer at minimum |
| IaC | Terraform for all infrastructure; modules per workstream | Reviewable, reproducible |
| Repo | Single monorepo, structured per §5.1 | Simpler dependency mgmt at this scale |
| CI/CD | Cloud Build triggered on PR; required green checks before merge | Standard |

**Naming conventions** (enforce in lint):
- GCP resources: `asb-{workstream}-{purpose}` (e.g., `asb-sync-airtable-sa`, `asb-agent-triage-engine`)
- BigQuery tables: `snake_case`, dataset prefix denotes ownership
- Python modules: `agency_brain.{workstream}.{component}`
- Terraform modules: `terraform/modules/{workstream}/`
- Pub/Sub topics: `asb-{purpose}` (e.g., `asb-triage-input`, `asb-routing-failures`)
- Reasoning Engines: `asb-agent-{name}` (e.g., `asb-agent-triage`, `asb-agent-risk-watcher`)

## 4. Security Charter

This section is the security posture for the entire build. Every workstream inherits these requirements.

### 4.1 The HIPAA boundary (defense in depth)

HIPAA-flagged clients must not appear in any Brain table, agent output, log, or notification. Five enforcement layers:

1. **Project boundary.** The HIPAA GCP project is not a peer; the Brain's service accounts have no IAM grants in it. Verified by an org-policy `iam.allowedPolicyMemberDomains` constraint and a daily `audit/hipaa_iam_drift.py` script.
2. **Sync filter.** `airtable_to_bq_sync` excludes any Client record with `HIPAA = true` and any Project/Task linked to such a client. Filter applied at the source query, not post-hoc.
3. **Workspace scoping.** Gmail connector uses a domain/label filter that excludes HIPAA-client domains. Drive monitored-folder list excludes HIPAA folders. Both filters are codified in Terraform and reviewed in PRs.
4. **Agent context guard.** Every agent invocation includes a pre-flight check: if any input record carries a `hipaa_excluded` aspect from Knowledge Catalog, the invocation aborts and emits a `HIPAA_GUARD_TRIPPED` audit event.
5. **Continuous audit.** `audit/hipaa_isolation_check.py` runs hourly via Cloud Scheduler. It joins every Brain table against the canonical HIPAA client list and alerts on any match. A match halts all agent execution via a feature flag in Secret Manager.

### 4.2 Service account topology + Agent Identity

No shared service accounts. Each agent, sync flow, and routing flow gets its own SA with a custom IAM role containing only the permissions it needs. Define roles in Terraform, never grant predefined roles like `roles/editor` or `roles/bigquery.admin`.

**Agent Platform addition:** Each deployed Reasoning Engine also receives an **Agent Identity** — a unique cryptographic ID assigned by the platform. Audit log rows include both the SA email and the Agent Identity. This is the platform-native version of the per-agent attribution we'd planned to build manually.

| SA | Permissions (custom role contents) |
|---|---|
| `asb-sync-airtable-sa` | BigQuery writes to `airtable_replica.*` only; Secret Manager read on Airtable token only |
| `asb-sync-vantage-sa` | BigQuery query on authorized Vantage view; BigQuery write to `vantage_kpi_snapshots` only |
| `asb-agent-triage-sa` | Pub/Sub subscribe on `asb-triage-input`; BigQuery write to `agent_outputs.triaged_items`; BigQuery audit-log write; Workspace read scopes via DWD subject; Memory Bank read/write on triage namespace |
| `asb-agent-risk-watcher-sa` | BigQuery query on `airtable_replica.*` and `agent_outputs.*` and `vantage_kpi_snapshots`; BigQuery write to `agent_outputs.triaged_items` and `agent_outputs.risk_flags`; Memory Bank read/write on risk-watcher namespace; audit log write |
| `asb-agent-goal-steward-sa` | BigQuery RW on `agent_outputs.goals*`; Memory Bank read/write on goal-steward namespace; Workspace Chat scope for prompts |
| `asb-agent-brief-sa` | BigQuery read on relevant tables; Gmail compose-only scope via DWD; audit log write |
| `asb-agent-knowledge-sa` | Knowledge Catalog query; BigQuery query (read-only); Model Garden invoke for Claude Sonnet |
| `asb-routing-sa` | BigQuery read on `agent_outputs.*`; Pub/Sub publish on per-channel topics; Gmail compose-only via DWD |

### 4.3 OAuth scopes (DWD)

Use the **narrowest available scope per agent**. The compose scope is critical — never grant `gmail.send` or `gmail.modify` to any SA. Default DWD subject is a dedicated, non-human Workspace user (`brain-agent@example.com`) with no mailbox of its own; per-user reads happen via subject impersonation with audit logging.

```
asb-agent-triage-sa:        drive.readonly, gmail.readonly, calendar.readonly, chat.spaces.readonly
asb-agent-risk-watcher-sa:  gmail.readonly, calendar.readonly  (no drive write needed)
asb-agent-brief-sa:         gmail.compose  (drafts only — explicitly NOT gmail.send)
asb-agent-knowledge-sa:     drive.readonly, gmail.readonly, calendar.readonly
asb-agent-goal-steward-sa:  chat.spaces (read+write — needs to send prompts in Chat)
```

Each scope grant in Workspace admin is documented in `docs/dwd_scopes.md` with the requesting SA, justification, and date. PRs that change scope require explicit reviewer approval.

### 4.4 Model Armor (prompt-injection defense)

Agents that ingest external content must have **Model Armor** enabled via `ModelArmorConfig`:

- **Triage Agent** — ingests Gmail body content from any sender, including untrusted parties. High-priority defense.
- **Knowledge Surfacer** — query handler that may receive untrusted input via the Gemini Enterprise app. Defense against query-injection that tries to surface HIPAA-excluded content.
- **Risk Watcher** — reads Gmail content for communication-pattern signals. Lower priority because it doesn't act on content, only counts and timestamps it, but enabling Model Armor is cheap and uniform.

Other agents (Goal Steward, brief composers, weekly synthesizer) operate on already-classified data from Brain tables, not external content. Model Armor not required.

### 4.5 Secrets

- All credentials (Airtable PAT, Workspace DWD client config, any LLM API keys for Claude) live in Secret Manager.
- Secrets are accessed at runtime, never baked into container images, never logged.
- `pre-commit` hook with `detect-secrets` blocks commits containing high-entropy strings.
- Airtable PAT and any LLM API keys rotated every 90 days; rotation runbook lives at `docs/runbooks/secret_rotation.md`.

### 4.6 Audit logging

Two layers:

1. **Cloud Audit Logs** (Admin Activity, Data Access, System Event) enabled on all services with 18-month retention. Logs sink to a separate, write-once project owned by the operator only.
2. **Application audit log** at `agent_audit_log.events`. Every agent invocation writes a row with: `event_id, timestamp, agent_id, agent_identity_uuid, sa_email, input_summary, output, confidence, latency_ms, cost_usd, hipaa_guard_status, model_armor_findings`.

The platform's **Agent Observability** provides execution traces and reasoning visualization automatically — no custom dashboards needed for that. The application audit log above is for the structured event data the routing layer and §13 metrics depend on.

### 4.7 The "drafts only" boundary (load-bearing)

No agent has any send/publish/modify capability against external systems. Concretely:

- Gmail: `gmail.compose` only (drafts folder of the impersonated user)
- Airtable: writes are limited to `agent_outputs/*` tables; never to source tables (Clients, Projects, Tasks-as-edited-by-humans)
- Calendar/Chat: read-only except Goal Steward's Chat write
- No webhook endpoints. No outbound HTTP from agents except to Google APIs (enforced by IAM scoping — agent SAs have grants only on the specific Google APIs they need)

A `audit/drafts_boundary_check.py` script runs nightly and verifies no agent SA has acquired a forbidden scope or role since the last check.

### 4.8 PR-level security gates

Every PR must pass:
- `terraform validate` + `tflint` (catches syntax + style)
- `detect-secrets` baseline check
- `scripts/least_privilege_check.py` — diffs IAM bindings and fails if any binding grants a predefined role like `editor`, `owner`, or `*.admin`
- `scripts/hipaa_filter_check.py` — confirms any SQL touching client data carries the standard HIPAA exclusion clause
- `scripts/model_armor_check.py` — confirms agents listed in §4.4 have `ModelArmorConfig` set in their Reasoning Engine definition

---

## 5. Workstream Decomposition (Parallelization Plan)

The spec presents a 12-week sequential plan. In practice, the work factors into **7 workstreams** that multiple Claude Code instances can run in parallel after a short shared foundation.

### 5.1 Repository structure

Create directories on first use rather than scaffolding everything up front.

```
agency-brain/
├── README.md
├── PRD.md                              # this document
├── docs/
│   └── source/                         # the original spec + goal hierarchy
├── terraform/
│   ├── envs/prod/
│   └── modules/
│       ├── foundation/                 # project, IAM, org policies
│       ├── data_pipeline/              # BigQuery, Pub/Sub, Application Integration
│       ├── agent_runtime/              # Reasoning Engines, Memory Bank, Schedules
│       ├── routing/                    # delivery channel infra
│       ├── observability/              # log sinks, metrics, alerts
│       └── security/                   # audit jobs, org policies, Model Armor configs
├── src/agency_brain/
│   ├── common/                         # shared models, audit log client, memory bank client
│   ├── sync/
│   │   ├── airtable_to_bq.py
│   │   ├── vantage_federation.py
│   │   └── workspace_to_pubsub.py
│   ├── agents/
│   │   ├── base.py                     # base agent: HIPAA guard, audit, prompt loading
│   │   ├── triage/
│   │   ├── risk_watcher/
│   │   │   ├── ecommerce.py
│   │   │   ├── local.py
│   │   │   └── agency_partner.py
│   │   ├── goal_steward/
│   │   ├── morning_brief/
│   │   ├── evening_reflection/
│   │   ├── knowledge_surfacer/
│   │   └── weekly_synthesizer/
│   ├── routing/
│   │   ├── matrix.py                   # severity → channel rules
│   │   └── channels/                   # one adapter per channel
│   └── audit/                          # the audit/check scripts referenced in §4
├── prompts/                            # agent prompts as separate files (versioned, reviewable)
├── airtable/
│   ├── schema.json                     # source of truth for the Airtable schema
│   └── validation_rules.md
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/                       # synthetic Workspace data, no real PII
└── scripts/                            # least_privilege_check, hipaa_filter_check, model_armor_check
```

### 5.2 Running multiple Claude Code instances

The workstream design optimizes for clean parallel execution across multiple Claude Code instances. The rules:

- **One workstream per Claude Code instance.** Each instance owns its workstream's branch end-to-end. No cross-workstream commits.
- **Branches are scoped by workstream.** `ws-a-foundation`, `ws-b-data-pipeline`, `ws-g-triage`, etc. Long-lived until the workstream merges.
- **Disjoint file ownership.** The repo structure in §5.1 is designed so each workstream owns its own subtree (`terraform/modules/foundation/` for WS-A, `src/agency_brain/sync/` for WS-B, `src/agency_brain/agents/triage/` for WS-G1, etc.). Cross-workstream changes go through the human reviewer, not direct edits.
- **Shared interfaces are versioned and merged early.** Anything multiple instances depend on (the base agent class, the audit log schema, the BigQuery table definitions for `agent_outputs.*`, the Memory Bank namespace conventions) merges to main as the first PR of the workstream that owns it. Other instances pull from main rather than coordinating cross-branch.
- **The integration points are the queue topics, table schemas, and Memory Bank namespaces.** As long as those don't change, instances don't step on each other.

### 5.3 Dependency graph between workstreams

```
        [WS-A: Foundation]
              │
       ┌──────┼──────────────┬────────────┐
       ▼      ▼              ▼            ▼
   [WS-B]  [WS-C]         [WS-E]       [WS-F]
   Data    Agent          Observ-      Security
   Pipe    Runtime        ability      Audits
       │      │              │            │
       └──┬───┘              │            │
          ▼                  │            │
      [WS-D: Routing]        │            │
          │                  │            │
          ▼                  │            │
  ┌───────┴────────┐         │            │
  ▼                ▼         │            │
[WS-G1: Triage]  [WS-G2..7: other agents — parallel within WS-G]
                           │
                           └────────── feeds → ──── WS-E, WS-F continuously
```

### 5.4 The seven workstreams

| ID | Name | Depends on | Can start | Est. wall-clock |
|---|---|---|---|---|
| **WS-A** | Foundation (project, IAM, repo skeleton, CI) | — | Day 1 | 1 week |
| **WS-B** | Data Pipeline (Airtable schema + sync, Vantage federation, BQ datasets, Knowledge Catalog reg) | WS-A | End of week 1 | 2 weeks |
| **WS-C** | Agent Runtime (base agent class, Memory Bank conventions, Reasoning Engine deployment patterns, Model Armor config templates) | WS-A | End of week 1 | 1.5 weeks |
| **WS-D** | Routing & Delivery (channel adapters, severity matrix, owner/leadership view logic) | WS-B (schemas), WS-C (event format) | Mid week 2 | 1.5 weeks |
| **WS-E** | Observability (audit log sink, dashboards, cost monitoring, on-call alerts) | WS-A | End of week 1 | continuous |
| **WS-F** | Security & Compliance (HIPAA isolation scripts, DWD scope docs, drafts boundary check, Model Armor verification, rotation runbooks) | WS-A | End of week 1 | continuous |
| **WS-G** | Agents — internally parallel after WS-C ships | WS-B + WS-C + WS-D | Week 3 | 6 weeks (in parallel) |

### 5.5 Parallelization within WS-G (the agents)

Once the runtime is stable, the seven agents factor into **three parallel tracks**, each ownable by its own Claude Code instance:

**Track 1 — Inbound classification (depends only on runtime + sync)**
- WS-G1: Triage Agent
- WS-G2: Risk Watcher with E-commerce profile
- WS-G2b: Local profile (parallel to G2 once base risk watcher ships)
- WS-G2c: Agency Partner profile (parallel to G2)
- WS-G2d: Acknowledgment Gap detector (parallel to G2)

**Track 2 — Outbound artifacts (depends on runtime + routing)**
- WS-G3: Morning Brief Composer (owner view first, leadership view second)
- WS-G4: Evening Reflection Composer
- WS-G5: Weekly Synthesizer

**Track 3 — Goals + Knowledge (independent of inbound/outbound)**
- WS-G6: Goal Steward
- WS-G7: Knowledge Surfacer

With three Claude Code instances on three tracks, the agent build compresses from 9 weeks (sequential) to roughly 4 weeks. Within Track 1, the three Risk Watcher profiles can each run as their own sub-instance once `risk_watcher/base.py` is merged.

### 5.6 Revised milestone schedule

| Week | Milestone (parallel work) |
|---|---|
| 1 | WS-A complete. WS-B, WS-C, WS-E, WS-F kicked off. Goal hierarchy entered into Airtable manually (no code dependency). |
| 2 | WS-B sync running 48h clean. WS-C base agent class merged with Memory Bank conventions. WS-D started. WS-E audit log table receiving events. WS-F HIPAA isolation script operational. |
| 3 | WS-D routing live. WS-G1 (Triage) shipped to its first Reasoning Engine revision with Model Armor enabled. Tracks 2 and 3 begin in parallel. |
| 4 | WS-G2 (Risk Watcher e-comm) live with Memory Bank baselines. WS-G3 (Morning Brief, owner view) in dev. WS-G6 (Goal Steward) in dev. |
| 5 | **the operator + partner using Brain daily (morning brief).** WS-G2b/c/d shipped. WS-G4 in dev. |
| 6 | WS-G2 all three profiles validated for 7 days. WS-G4 (Evening Reflection) live. Leadership view live. |
| 7 | WS-G5 (Weekly Synthesizer) live. WS-G6 (Goal Steward) running first weekly cadence. |
| 8 | WS-G7 (Knowledge Surfacer) live with Claude Sonnet via Model Garden. Drafts capability validated end-to-end. |
| 9–10 | Tuning, false-positive reduction, prompt iteration. Cost optimization pass (Cached Contents review). |
| 11 | Hardening: chaos-test the HIPAA boundary. Rotate all secrets to confirm runbooks work. Test Model Armor against known prompt-injection patterns. |
| 12 | Internal security review against §4 charter. PRD reflects implementation. Documentation complete. |

This is **3 weeks faster than the spec's serial plan** and produces shippable subsets at every milestone.

---

## 6. Per-Component Specifications

This section covers what the spec leaves under-specified for an implementing agent. For agent behavior, prompts, and triggers, defer to spec §5–§9.

### 6.1 Base agent class (WS-C)

`src/agency_brain/agents/base.py` — every agent subclasses this. It provides:

- **HIPAA pre-flight check:** aborts with `HIPAA_GUARD_TRIPPED` if any input has the `hipaa_excluded` aspect
- **Audit log emission:** structured row to `agent_audit_log.events` on every invocation, success or failure (includes Agent Identity UUID from the platform)
- **Prompt loading:** prompts loaded from `prompts/` directory, version pinned, never inline strings
- **Confidence threshold enforcement:** invocations returning `confidence < 0.7` are auto-routed to human review queue regardless of agent intent
- **Memory Bank namespace helpers:** standard read/write methods using the conventions established in WS-C's first PR

Cost tracking, retries, and circuit breakers are not in the base class. The Agent Runtime SDK handles transient retries, and Agent Observability tracks cost automatically. Add custom retry logic later only if a specific agent needs it.

### 6.2 Sync flows (WS-B)

One Airtable base (per ADR 0020, supersedes ADR 0011's two-base architecture): **Operations** (`appXXXXXXXXXXXXXX`) carries everything — Accounts (the canonical client record), Contacts, Contracts, Projects, Tasks, Goals, Goal Scores, Team, Risk Profiles, Service Catalog. The collapse retired the legacy Sales/CRM base + the cross-base text-field-recordID joins; all relationships are real linked records.

Implementation: per ADR 0010, the Airtable sync runs as a Cloud Run Job triggered by Cloud Scheduler every 15 minutes (chosen over spec §9.1's Application Integration). Other sync flows (Vantage federation, Workspace events) ship in later PRs and may use Application Integration or Pub/Sub-triggered Cloud Functions as appropriate. Each flow has its own `asb-sync-*-sa` with a custom IAM role.

The Airtable sync deserves extra care because it's the operational spine:

- **Pull strategy:** WRITE_TRUNCATE per table per cycle (replica tables are small; full snapshot is the simplest correct thing). `_sync_checkpoints` table tracks per-table run metadata; the `last_modified > checkpoint` plumbing exists in `hipaa_filters.last_modified_after` for a future incremental MERGE-based evolution. ADR 0010 records the design.
- **Idempotency:** WRITE_TRUNCATE produces the same end state on re-run by construction. MERGE is the future evolution if row counts grow.
- **HIPAA filter:** applied at the Airtable API query level via Lookup fields (`Projects.Account HIPAA`, `Tasks.Project HIPAA`, `Contacts.Account HIPAA`, `Contracts.Account HIPAA`) and `filterByFormula`, not in post-processing. The cascade is wired and inert today (no HIPAA-flagged accounts); flipping `HIPAA = true` on an Account removes it and dependent rows within one sync cycle. Verified by `tests/security/test_hipaa_isolation.py` (unit) and `docs/runbooks/hipaa_isolation_verification.md` (live).
- **Schema drift:** new columns logged to `asb-schema-drift-alerts` Pub/Sub topic, surfaced to the human admin, **never auto-added**. PRs to add columns require explicit human approval.
- **Source of truth:** `airtable/schema.json` is the spec from which the Operations base is built (manual one-time setup per `docs/runbooks/airtable_operations_base_setup.md`). The Python sync code and Terraform replica DDL both derive their column layouts from this file.

### 6.3 Triage Agent (WS-G1)

The classification contract from spec §7 is the source of truth. Implementation notes:

- Prompt lives in `prompts/triage_v1.md`. Versioned. Diffed in PRs.
- Goal context is loaded via **Cached Contents** — active goals from `agent_outputs.goals` (filtered to `Status = Active`, `Horizon ∈ {Quarterly, 1-year}`) are cached per worker for 5 minutes. This is the highest-leverage cost optimization since goal context is identical across thousands of Triage invocations per day.
- Ownership lookup: a small in-memory dict from a daily-refreshed materialized view `airtable_replica.account_owners_v` (renamed from `client_owners_v` in ADR 0020). Never query Airtable directly from inside an agent.
- **Model Armor enabled** — Triage ingests untrusted Gmail content.
- Output goes to `agent_outputs.triaged_items` table. Routing layer reads from this table.
- Deployed as a Reasoning Engine; new prompt versions ship as `runtimeRevisions` for safe rollback.

### 6.4 Risk Watcher (WS-G2 + variants)

**Pattern:** one base `RiskWatcher` class, three subclasses for the segment profiles. Each profile encodes its trouble signals as a Python list of `Signal` objects, each with a `def evaluate(client_state) -> Optional[Flag]`. This makes the profiles diffable, testable, and tunable independently — and lets WS-G2b and WS-G2c be built by different Claude Code instances without merge conflict (each adds a new file under `risk_watcher/`).

```python
# pseudo-shape, not literal code
class Signal:
    name: str
    severity: Severity
    def evaluate(self, client_state: ClientState) -> Optional[Flag]: ...

class EcommerceProfile(RiskProfile):
    signals = [
        AcknowledgmentGapSignal(threshold=0.20, days=5, severity=Severity.CRITICAL),
        SilentAfterDeliverableSignal(business_days=5, severity=Severity.HIGH),
        # ...
    ]
```

**Memory Bank usage:** per-client baselines (rolling 8-week ROAS, response latency, etc.) live in Memory Bank under namespace `risk-watcher/{client_id}/baseline`. The agent reads on each invocation and writes updated baselines after evaluation. This replaces the BigQuery table approach considered in spec §14.

The Acknowledgment Gap detector (WS-G2d) is a cross-cutting signal that runs across all three profiles. Implement as a separate module `risk_watcher/acknowledgment_gap.py`, scheduled independently per spec §9.2.

### 6.5 Routing layer (WS-D)

`src/agency_brain/routing/` — runs as a Cloud Function triggered by BigQuery streaming inserts on `agent_outputs.triaged_items` (with a polling fallback every 5 min for reliability).

- `matrix.py`: pure-function severity-to-channel mapping. Easy to unit test.
- `channels/{gmail,chat,airtable,gemini_inbox}.py`: one adapter per channel, conforming to a `Channel` protocol (`send(item, view)`).
- Owner-view vs leadership-view is computed by the routing layer, not by the agents. Agents always emit the full classified item; the routing layer decides who sees what.
- Failed sends go to `asb-routing-failures` topic with the original item attached. A daily report summarizes failures.

### 6.6 Knowledge Surfacer (WS-G7)

The strictest precision posture. Implementation requirements:

- **Model: Claude Sonnet via Model Garden.** The conservative refusal posture aligns with the precision requirement. Spec §14's open question on agent model choice is closed for this agent. Configure via Model Garden's Anthropic integration; key in Secret Manager.
- **Model Armor enabled** — guards against query-injection that tries to surface HIPAA-excluded content.
- Every response includes citations resolving to record IDs or document IDs.
- Refuses politely on out-of-scope queries (HIPAA-flagged clients, future predictions, anything requiring write access).
- Query authorization in v1 is binary (the operator-only). v2 multi-user auth is out of scope (per §2).
- Hallucination guardrail: if the model's response contains entity references (client names, project names, dates) not present in retrieved context, the response is rewritten or refused.
- v1 retrieval layer is **Knowledge Catalog**, not RAG Engine (see Appendix C.7).

---

## 7. Testing Strategy

Three test layers. All run in CI on every PR.

### 7.1 Unit tests
- Pure functions (routing matrix, signal evaluators, classification post-processors)
- Target: > 80% coverage on `src/`, 100% on `routing/matrix.py` and `audit/*`
- No live GCP calls; mock the GCP clients

### 7.2 Integration tests
- Run against ephemeral test resources spun up and torn down by the test itself (no permanent dev environment to drift)
- Synthetic Workspace data (no real PII) seeded into a dev Workspace tenant
- Each agent has a "golden classification set" — ~50 hand-labeled inputs with expected outputs. **Use Agent Platform's Agent Evaluation** to score against this set; regression fails the build.
- Sync flows: end-to-end test that an Airtable change appears in BigQuery within the SLA

### 7.3 Security tests (`tests/security/`)
- HIPAA isolation: assert that flipping `HIPAA = true` on a test client removes them from all Brain tables within one sync cycle
- Drafts boundary: assert that no SA has any send/modify scope; failure if a forbidden scope appears
- Model Armor: assert that Triage Agent rejects a curated set of known prompt-injection patterns

(The least-privilege check is in the CI gate at §4.8, not duplicated here.)

### 7.4 Acceptance criteria per workstream

Each workstream has a specific "definition of done" enumerated in `docs/acceptance/{workstream}.md`. A workstream is not merged to main until its acceptance file is checked off by the operator.

---

## 8. Observability (WS-E)

### 8.1 Required dashboards

**Agent Platform provides natively** (no custom build):
- Agent execution traces and reasoning visualization (Agent Observability)
- Per-agent invocations, error rates, p50/p95 latency, cost (built-in metrics)

**Custom BigQuery-driven dashboards**:
- **Classification quality:** confidence-score distribution, dismissal rate vs. confirmation rate, false-positive rate per Risk Profile
- **Sync health:** lag per source, schema-drift events, HIPAA filter exclusions per cycle
- **Cost:** monthly projection vs. budget in spec §15 (supplements platform-native cost view)
- **Security:** count of `HIPAA_GUARD_TRIPPED` events (must be zero), DWD scope drift, IAM binding changes, Model Armor findings count

### 8.2 Required alerts

| Signal | Threshold | Channel |
|---|---|---|
| Any HIPAA isolation breach | ≥ 1 event | Chat DM to the operator + halt agent execution |
| Sync flow failure | 2 consecutive failures | Email + Chat to the operator |
| Agent error rate | > 10% over 1h | Chat to the operator |
| Audit log write failure | ≥ 1 event | Email + halt agent execution |
| Model Armor block rate spike | > 5x baseline over 1h | Email (possible attack or misconfiguration) |

A cost-anomaly alert is added in week 9–10 once there's a stable baseline to compare against — adding it earlier creates noise during prompt tuning.

---

## 9. Cost Controls

- Knowledge Catalog discovery scans run twice daily (6 AM, 6 PM Pacific) per spec §10.3 — configured in Terraform, not by hand.
- Triage Agent uses Gemini 3.1 Flash; only escalates to 3.1 Pro when `confidence < 0.7` on initial pass.
- **Cached Contents** for stable per-invocation context: active goals (Triage), client baselines (Risk Watcher). Materially reduces token cost for high-volume agents.
- Reasoning Engines scale to zero outside business hours where possible (configured per agent based on cadence — Risk Watcher and Goal Steward run on schedules; Triage and routing must remain warm).

## 10. Documentation Requirements

- `docs/runbooks/` updated for any new operational procedure (created on first runbook, not pre-scaffolded)
- `docs/adr/` entry for any architectural decision deviating from this PRD or the spec (created on first ADR)
- `README.md` in each `src/` subdirectory explaining what the module does and how to test it
- This PRD updated if implementation reveals required deviations (PRD changes are themselves PRs)

## 11. Definition of Done (the v1 launch criteria)

The build is "done" when **all** of the following are true:

1. All 7 agents are deployed as Reasoning Engines in prod and have run successfully for 7 consecutive days
2. the operator and partner are receiving morning briefs and evening reflections daily for ≥ 14 days
3. At least one Risk Watcher flag has been confirmed by the operator as a real catch
4. Goal scores recorded weekly for ≥ 4 weeks
5. Audit log contains zero `HIPAA_GUARD_TRIPPED` events
6. Model Armor has blocked at least one synthetic prompt-injection test successfully (proves it's wired correctly)
7. All security tests pass; least-privilege check passes; drafts boundary check passes
8. Cost over the prior 30 days is within 20% of the spec §15 projection
9. All 7 workstreams' acceptance docs are signed off
10. Disaster recovery: a documented and tested runbook exists for restoring from BigQuery snapshots, rolling back a Reasoning Engine to a previous `runtimeRevision`, and rotating all secrets under 4 hours
11. Internal security review against §4 charter completed with no high-severity findings open

## 12. Handoff Notes for Claude Code Instances

When implementing from this PRD:

1. **One workstream per instance.** Each Claude Code instance owns its workstream's branch from kickoff to merge. Do not cross workstreams.
2. **Start with WS-A end-to-end** before any other workstream begins. The foundation must be right before parallel work fans out.
3. **Pull shared interfaces from main, don't coordinate cross-branch.** When WS-C merges the base agent class and Memory Bank conventions, agent-track instances rebase from main.
4. **Treat the security charter (§4) as immutable.** If a section of code seems to require violating it, stop and write an ADR explaining why, rather than relaxing it.
5. **Prefer Terraform over scripts** for any infrastructure change. If a one-off script is needed, document it in `scripts/` with a note on whether it should become Terraform.
6. **Never commit a secret.** If something looks like it might be a secret, it is — route it through Secret Manager.
7. **Write the test before the integration.** Especially for security-sensitive paths (HIPAA filter, drafts boundary, IAM bindings, Model Armor configs).
8. **Use platform primitives over custom code.** Memory Bank > custom BigQuery state tables. Cached Contents > custom caching. Agent Observability > custom traces. The only reason to roll a custom alternative is a documented platform limitation.
9. **Ask the operator via PR comment** when the spec and this PRD are silent or in tension. Do not invent behavior. The spec's §14 lists open decisions that may apply.
10. **Update this PRD** when implementation reveals required deviations. The PRD is a living document for v1; freeze it for v2.

---

## Appendix A: Workstream Kickoff Checklist

For each workstream, before writing code:

- [ ] Acceptance doc created at `docs/acceptance/{workstream}.md`
- [ ] Terraform module skeleton at `terraform/modules/{workstream}/` (only if the workstream owns infrastructure)
- [ ] Source directory created with empty `__init__.py` and a `README.md`
- [ ] Branch created: `ws-{id}-{shortname}`
- [ ] CI green on the new branch (no functionality, but builds and lints pass)
- [ ] At least one ADR drafted if the workstream involves any non-obvious decision

## Appendix B: Open Items Inherited from Source Documents

These were listed in the spec §14 and goal hierarchy "Open Items." Status updated based on Agent Platform decisions:

| Item | Status |
|---|---|
| Vantage cross-project topology | Decide week 1 of WS-A |
| Per-agent model choice | Defaults set in §3 (Gemini 3.1 Pro/Flash; Claude Sonnet for Knowledge Surfacer); revisit only if performance demands it |
| Memory Bank vs. BigQuery for risk baselines | **Resolved: Memory Bank.** First-class Agent Platform feature with the persistence semantics needed |
| Peninsula revenue floor | Goal Steward (WS-G6) must surface this as a flag at day 90 post-launch |
| Service standards codification | Out of scope for v1 |

## Appendix C: Considered & Rejected for v1

Decisions made deliberately, recorded so they don't get re-debated mid-build. Promote to ADRs in `docs/adr/` when the repo exists.

### C.1 VPC Service Controls — rejected

**What it would do:** Draw a perimeter around GCP services so credentials, even if stolen, can't exfiltrate data to projects outside the perimeter.

**Why not in v1:** The Brain's threat model is dominated by (a) accidental HIPAA leakage through a misconfigured filter, (b) an agent SA acquiring too-broad scope, and (c) leaked third-party tokens. VPC-SC mitigates none of these directly — they're addressed by the HIPAA defense-in-depth (§4.1), least-privilege IAM (§4.2), and Secret Manager + rotation (§4.5).

**What we'd need to revisit this:** v2 if the Brain ever ingests client PII directly (rather than aggregated KPIs from the Vantage authorized view), or if the Brain becomes a multi-tenant product per spec §17.

### C.2 Customer-Managed Encryption Keys (CMEK) — rejected

**What it would do:** Replace Google-managed encryption keys with keys you control in Cloud KMS.

**Why not in v1:** Operational overhead is real (key rotation runbooks, key-availability incidents that look like data-loss events, IAM on the key itself becoming another surface to manage) and the marginal security benefit over Google-managed keys is small for an internal operational tool.

**What we'd need to revisit this:** Compliance regime that explicitly requires customer-managed keys.

### C.3 Cloud DLP scrubbing on audit log writes — rejected

**What it would do:** Pass every agent output through Cloud DLP before writing to the application audit log to redact PII.

**Why not in v1:** The audit log lives in your project, behind your IAM, accessed only by the operator. DLP adds latency and cost to every agent invocation to defend against PII appearing in logs only the operator reads.

### C.4 Permanent dev environment mirror — rejected

**What it would do:** Maintain a `agency-brain-dev` GCP project as a full mirror of prod for integration testing.

**Why not in v1:** For a two-person operation, dev environment drift becomes its own problem. Integration tests spin up ephemeral test resources and tear them down — same coverage, no parallel infra to maintain.

### C.5 External security audit — rejected

**What it would do:** Hire a third-party firm to pentest the Brain.

**Why not in v1:** Cost-prohibitive for a two-person internal tool, and findings tend to be mostly irrelevant to the actual threat model. Replaced by an internal review against the §4 charter at week 12.

### C.6 BigQuery scan lint rule — rejected

**What it would do:** Lint rule rejecting unbounded BigQuery scans to control query costs.

**Why not in v1:** BigQuery's free tier is 1 TiB/month of queries. At ~50k events/month against tiny tables, you won't hit it.

### C.7 RAG Engine for Knowledge Surfacer — deferred to v2

**What it would do:** Replace Knowledge Catalog as the retrieval layer with Agent Platform's RAG Engine (`ragCorpora` + `ragFiles`).

**Why not in v1:** RAG Engine is purpose-built for retrieval and likely outperforms Knowledge Catalog on relevance. But the HIPAA boundary's defense-in-depth layer 3 (§4.1) depends on Knowledge Catalog aspects (`hipaa_excluded`). Switching to RAG Engine in v1 means re-architecting the HIPAA enforcement at retrieval time. The Knowledge Surfacer ships in week 8, leaving room to evaluate without blocking. Worth the swap in v2 once we've seen Knowledge Catalog's actual relevance performance and can design the HIPAA enforcement cleanly for RAG Engine.

### C.8 Agent Studio (low-code) for any agent — rejected

**What it would do:** Build the simpler agents (Goal Steward in particular) in Agent Studio's low-code visual environment instead of ADK.

**Why not in v1:** Splits the codebase across two build environments, complicates testing and version control. Goal Steward is mostly cadence + structured prompts but ADK handles that fine. Stay with ADK for everything in v1; revisit only if Agent Studio gains capabilities ADK lacks.

### C.9 Agent Sandbox for agents that need code execution — deferred (not rejected)

**What it would do:** Use Agent Platform's Agent Sandbox (hardened environment for model-generated code execution) for the Knowledge Surfacer if it ever needs to compute against retrieved data.

**Status:** No agent in v1 needs code execution. Add only if a specific use case emerges.

---

*End of PRD v1.2. Companion documents: `proactive_agency_brain_spec.md` (design), `agency_goal_hierarchy_v0.1.md` (strategic context). All three live together in `docs/source/`.*
