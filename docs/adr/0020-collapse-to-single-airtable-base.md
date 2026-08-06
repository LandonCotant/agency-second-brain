# ADR 0020 — Collapse to a single Airtable base

**Status:** Accepted
**Date:** 2026-04-29
**Workstream:** WS-B (Data Pipeline) — affects every workstream that reads `airtable_replica`
**Supersedes:** ADR 0011 (two-base architecture), ADR 0021 (CRM read-sync, never implemented)
**Related:** ADR 0010 (Cloud Run Job for sync — host model unchanged), ADR 0022 (Triage no-match policy + Owner-id sync — unchanged)

## Context

ADR 0011 (2026-04-27) chose a two-base Airtable architecture: **Sales/CRM** (`appj1Bo8Uw6Oa6MwE`, pre-existing) for sales records and a new **Operations** base (`appXXXXXXXXXXXXXX`) for delivery (Projects, Tasks, Goals, Team, Risk Profiles, Service Catalog, plus a thin Clients reference). The split was chosen to:

1. Preserve the user's existing Sales/CRM base unchanged.
2. Keep sales hygiene and operational hygiene separable in the future.
3. Avoid speculative rework if the user later wanted scoped contractor access.

PR 1 (ADR 0021) implemented the cross-base sync: `airtable_replica.crm_*` tables, a separate `airtable-crm-pat-prod` PAT, and a multi-base `BaseConfig` orchestration in the sync code. PR 2 wired the Triage Agent's resolver on top of that.

Two weeks of build experience reveal the costs are real and the benefits are speculative for a 2-person tool:

| ADR 0011's reason | Reality at 2026-04-29 |
|---|---|
| "Sales hygiene and operational hygiene have different cadences" | Single founder. Cadence argument assumes a team where some people work in CRM and others in Ops. |
| "Independent evolution" | Hasn't materialized. Both bases evolve in lockstep with single-author edits. |
| "Access scoping for future contractors" | Speculative. Airtable's Interface Designer + share-link controls scope per-table within one base too. |
| "Cost of separation: cross-base linked records aren't supported" | This cost is real and load-bearing. `Clients.CRM Account Record ID` and `Projects.Source Contract Record ID` are text fields holding `recXXX` strings, joined at query time in BQ. The Operations.Clients table exists *only* as a denormalized cross-base mirror. |

The split also created concrete user friction we just hit while setting up PR 2:
- Two PATs to manage (`airtable-pat-prod` for sync, `airtable-crm-pat-prod` for the second base).
- A "sentinel client" had to be invented as a manual prerequisite for the Triage Inbox project (because Projects.Client is a required link and Operations.Clients was empty).
- The sender resolver's join chain crosses bases via text-field record IDs, which is fragile.
- Schema drift is two files (`schema.json` + `crm_schema.json`).

The pre-existing CRM base has minimal data: 2 Accounts, 2 Contacts, 3 Contracts, 0 Leads. Migration cost is trivial — the bet that justified the split (preserving an established CRM base) was right at the time but is small now.

## Decision summary

1. **One Airtable base.** Operations (`appXXXXXXXXXXXXXX`) absorbs Accounts, Contacts, Contracts, and Leads from the CRM base. The Sales/CRM base is archived (read-only, for history; not deleted).
2. **Drop the Operations.Clients table.** The "thin reference mirroring CRM Accounts" was a workaround for cross-base limitations. Operations.Accounts becomes the single client record, carrying both sales-side fields (Total Lifetime Value, Lead Source) and operational fields (Segment, Status, Account Manager, HIPAA).
3. **Cross-base text fields become real linked fields.** `Projects.Account` is a real `multipleRecordLinks` to Accounts. `Contracts.Account` is a real link to Accounts. `Tasks.Project` already was a real link.
4. **Two PATs, narrow scopes.** `airtable-pat-prod` (read-only, used by the sync). `airtable-tasks-write-pat-prod` (write-only on Tasks, used by the Triage Agent). The CRM-specific `airtable-crm-pat-prod` is retired.
5. **Single-base sync code.** Revert the multi-base `BaseConfig` infrastructure introduced in PR 1. The sync orchestrator goes back to one base, one schema file, one PAT, one Cloud Run Job.
6. **Materialized view simplifies.** `sender_to_project_v` joins `Contacts → Accounts → Projects` (drops the Clients middleman; one less JOIN). HIPAA filter still applies via the canonical clause.
7. **Defer Leads sync.** Currently 0 rows. Add when an agent (Risk Watcher renewal alerts, Goal Steward) needs pipeline context.

## Operations base — final 8 tables

| Table | Origin | Notes |
|---|---|---|
| **Accounts** | New (migrated from CRM) | One row per company. Carries sales fields + operational fields + HIPAA. Replaces Clients. |
| **Contacts** | New (migrated from CRM) | People at Accounts. JOIN target for sender resolution. |
| **Contracts** | New (migrated from CRM) | Real linked field to Accounts. |
| **Projects** | Existing | `Account` link replaces `Client` link + `Source Contract Record ID` text field. |
| **Tasks** | Existing | Unchanged shape; Project link still required. |
| **Goals** | Existing | Unchanged. |
| **Goal Scores** | Existing | Unchanged. |
| **Team** | Existing | Adds `User` field per ADR 0022 (singleCollaborator → `usrXXX`). |
| **Service Catalog** | Existing | Unchanged. |
| **Risk Profiles** | Existing | Unchanged. |

(Clients and Leads/Opportunities are absent; Clients merges into Accounts, Leads is deferred.)

## What survives from PR 1 + PR 2

About 70% of the work shipped in PR #31 (PR 1) and PR #33 (PR 2). The data-source change cascades but the agent-side architecture is unchanged.

| Component | Status under ADR 0020 |
|---|---|
| `ProjectResolver` class + tests | **Stays** — algorithm unchanged, query targets `accounts` instead of `crm_accounts` |
| `AirtableTasksWriteClient` | **Stays** — unchanged |
| TaskDrafter `owner_record_id → owner_user_id` rename | **Stays** |
| TriageAgent `_run()` wire-in + constructor invariant | **Stays** |
| ADR 0022 (no-match policy + Owner-id sync) | **Stays valid** |
| `_extract: "user_id"` annotation + Team.User field | **Stays** |
| Materialized view `sender_to_project_v` | **Simplified** — Contacts → Accounts → Projects (drop Clients hop) |
| `crm_schema.json` | **Deleted** — folded into single `schema.json` |
| Multi-base `BaseConfig` / `sync_all()` infra | **Reverted** — back to single-base `sync()` |
| `airtable-crm-pat-prod` secret + IAM binding | **Retired** |
| `crm_*` BQ tables | **Replaced** by plain `accounts`/`contacts`/`contracts` |
| `airtable_replica.clients` BQ table | **Deleted** — Operations.Clients table goes away |

## What was rejected

- **Status quo.** Pay the two-base tax indefinitely. Defensible if migration cost were high, but it isn't (7 rows total).
- **Keep a `Clients` table as a thin link to Accounts.** Adds a layer of indirection that was the whole problem in the first place. If everything's in one base, `Projects.Account` is the link; no middleman needed.
- **Sync Leads/Opportunities now.** 0 rows today; no agent reads them. Defer until a real consumer materializes.
- **Rename the base** from "Agency Operations" to something else. Optional; minimizes UI churn to keep the existing name. Can be a one-click rename later if it bothers anyone.

## Consequences

- PR #31 (CRM read-sync foundation) and PR #33 (resolver wire-in) are **closed as superseded**, not merged. The collapse PR replaces both.
- The Sales/CRM base in Airtable is **archived**, not deleted. Any downstream tool that referenced it will break — but no such tool exists in the v1 build.
- The `airtable-crm-pat-prod` Secret Manager secret remains in place as a no-op (deletion is irreversible; leaving it costs nothing). Optional cleanup later.
- Existing PRD §6.2 references to "two bases" need a one-line update pointing to this ADR.
- One Airtable base means one IAM scope to reason about, one schema-drift surface, one PAT-rotation runbook entry per role.

## Revisit if

- Contractors come on with sales-only or ops-only access requirements that table-level Airtable permissions can't satisfy.
- Leads/Opportunities grows to a state where a real agent needs to read it — sync it then.
- A second base becomes useful for a genuinely different domain (e.g. a client-facing portal that's intentionally separate from the operational center). At which point the cross-base text-field-record-id pattern is the lightweight workaround.
