# Airtable Operations Base — Validation Rules

Cross-field constraints and invariants for the Operations Airtable base. The field-level schema (types, options, linked tables) is in [`schema.json`](./schema.json); this file documents rules that span fields or change semantics.

Per [PRD.md](../PRD.md) §6.2 and [ADR 0011](../docs/adr/0011-airtable-operations-base.md).

## Mandatory fields (cross-field rules)

| Table | Field | Rule |
|---|---|---|
| Clients | CRM Account Record ID | Required. Must be a valid `recXXX...` from the Sales/CRM base Accounts table. The Brain validates on sync; missing CRM account → drift alert. |
| Clients | Account Manager | Required. Default Owner for Projects spawned for this client. |
| Projects | Client | Required. Single-link in practice. |
| Projects | Service | Required. Linked to Service Catalog (single source of truth for service offerings). |
| Projects | Source Contract Record ID | Required. Cross-base join to CRM Contracts. |
| Projects | Owner | Required. Defaults from `Client.Account Manager` when blank. |
| Projects | Health Reason | Required when `Health ∈ {Yellow, Red}`. What's off, recovery plan, when we expect Green again. |
| Tasks | Project | Required. Drives HIPAA cascade and ownership inheritance. |
| Tasks | Owner | Required. Brain-drafted Tasks default to `Project.Owner`; manual Tasks set explicitly. |
| Tasks | Wait Reason | Required when `Action Type = Wait`. |
| Tasks | Wait Started | Required when `Action Type = Wait`. Used by aging logic. |
| Goals | Owner | Required. Goal Steward prompts this person weekly. |
| Goals | Parent Goal | Required UNLESS `Horizon = 10-year`. |
| Goal Scores | Goal | Required. |
| Goal Scores | Week Of | Required. Monday of the week being scored. |
| Goal Scores | Score | Required. 1-10. |
| Team | Workspace Email | Required. JOIN KEY for Brain ownership resolution. |
| Service Catalog | Service Code | Required. Must be unique across rows. |

These are enforced in Airtable (required field settings, formula validation, or scripting block) before WS-G agents go live in week 3. Otherwise the Brain inherits inconsistent ownership data and routing fails silently.

## HIPAA flag (load-bearing)

`Clients.HIPAA` (checkbox) is the source of truth for HIPAA exclusion. **Currently false on every row** — the agency has no HIPAA-regulated clients today. The cascade machinery is wired and inert; flipping the flag removes the client and dependent rows from the BigQuery sync within one cycle.

The cascade reaches Projects and Tasks via Airtable Lookup fields — same pattern as the original spec, simpler in this base because Tasks links directly to Projects which links directly to Clients (no need for transitive lookups beyond `Tasks.Client` lookup-from-Project).

| Table | Field | How it cascades |
|---|---|---|
| Clients | `HIPAA` | Checkbox; manually set. Source of truth. |
| Projects | (no HIPAA field) | Excluded from sync via filterByFormula referencing `{Client}` link's HIPAA value (computed at query time). |
| Tasks | `Client` (Lookup) | Auto-derived from `Project.Client`. The HIPAA filter joins Tasks → Projects → Clients in the source query. |

The `airtable_to_bq` sync issues these `filterByFormula` clauses on every pull:

- **Clients** → `NOT({HIPAA})`
- **Projects** → `NOT({Client HIPAA})` *(requires a Lookup field `Client HIPAA` on Projects pulling `HIPAA` from the linked Client — see setup runbook)*
- **Tasks** → `NOT({Project HIPAA})` *(requires a Lookup field `Project HIPAA` on Tasks pulling `Client HIPAA` from the linked Project)*

The two Lookup fields (`Projects.Client HIPAA`, `Tasks.Project HIPAA`) are documented in the setup runbook. They're not in the primary schema.json field list because they're derived — but the runbook walks through creating them.

Rules:

- The `HIPAA` flag must NEVER be edited by the Brain or any agent. Manual control only. v1 enforces this through the sync SA's read-only Airtable PAT scope.
- The two Lookup fields are derived — never written. They must exist in the Airtable base before `airtable_to_bq` runs (otherwise every sync of Projects/Tasks returns zero rows because the formula references unknown fields).
- `tests/security/test_hipaa_isolation.py` asserts every outbound request carries the appropriate clause.

## Status enums (canonical option lists)

Single-select option order matches `schema.json`. Adding/removing values must happen in Airtable first; the sync's schema drift detector surfaces deltas to `asb-schema-drift-alerts`. **Never auto-add** (PRD §6.2).

Canonical lists are maintained in `schema.json`. The table below is a human-readable index — when in doubt, `schema.json` wins.

| Table | Field | Allowed values |
|---|---|---|
| Clients | Segment | E-commerce, Local Service, Agency Partner |
| Clients | Status | Onboarding, Active, Mature, Renewal Window, Churned |
| Projects | Phase | Discovery, Design, Build, Launch, Maintain, Closeout |
| Projects | Status | Not Started, Active, Blocked, Complete, Cancelled |
| Projects | Health | Green, Yellow, Red |
| Tasks | Source | Manual, Triage Agent, Risk Watcher, Goal Steward, Other |
| Tasks | Action Type | Do It Now, Delegate, Defer, Schedule, Wait |
| Tasks | Category | Calls, Computer, Errands, Office, Schedule, Team Meeting, Staff, Waiting For, Home |
| Tasks | Task Type | Task, Project |
| Tasks | Status | Open, In Progress, Done, Cancelled |
| Tasks | Approval Status | Approved, Drafted by Agent, Rejected |
| Goals | Horizon | 10-year, 3-year, 1-year, Quarterly, Weekly |
| Goals | Area of Focus | Revenue, Team, Product, Personal, Education, Other |
| Goals | Status | Active, Achieved, Abandoned, Paused |
| Team | Role | Leadership, Account Manager, Salesperson, Specialist, Contractor |
| Service Catalog | Category | Platform Subscription, Project-Based, Retainer |
| Service Catalog | Typical Engagement Length | One-time, 1-3 months, 3-6 months, 6-12 months, Ongoing |
| Risk Profiles | Segment | E-commerce, Local Service, Agency Partner |
| Risk Profiles | Severity Default | Critical, High, Medium, Low |

## Goal hierarchy invariant

A Goal's `Parent Goal` must have a longer horizon than the Goal itself:

- Weekly → Quarterly
- Quarterly → 1-year
- 1-year → 3-year
- 3-year → 10-year
- 10-year → (none — root of the hierarchy)

Enforced by the Goal Steward agent (WS-G6) at write time. The Airtable schema itself does not enforce this; the sync surfaces violations as drift events once the WS-G6 audit ships.

## Approval Status semantics

`Tasks.Approval Status` distinguishes Brain-drafted Tasks from human-approved ones. Routing matrix (WS-D) and downstream agents read this field:

| Value | Meaning | Created by |
|---|---|---|
| Approved | Task is part of the operational queue. Owner is responsible. | Manual creation OR human approving a Brain draft |
| Drafted by Agent | Task is pending human review. Owner sees it in their "Awaiting Approval" view; can approve, edit, or reject. | Triage / Risk Watcher / Goal Steward |
| Rejected | Brain draft was dismissed. Captured for false-positive analysis (PRD §13). Not actionable. | Human rejecting a draft |

WS-D routing only acts on `Approval Status = Approved` Tasks. Drafted Tasks are visible in Airtable for review but don't trigger downstream routing.

## Project lifecycle invariants

- A Project with `Status = Active` should have a non-blank `Owner`, a `Service` link, a `Source Contract Record ID`, and a `Phase` matching one of the active phases (Discovery / Design / Build / Launch / Maintain).
- A Project moves to `Status = Complete` only when `Phase = Closeout` AND `Actual End Date` is set.
- Health = Green is the default; Yellow/Red triggers a required Health Reason. Risk Watcher can flip Health from Green → Yellow/Red and writes a corresponding Task with `Source = Risk Watcher` to surface what's wrong.

## Project ↔ Contract relationship

A CRM Contract may spawn multiple Projects:

- A retainer Contract bundling SEO + Paid Ads + Consulting → 3 Projects (one per service line)
- A single-month Marketing Consulting retainer Contract → 1 Project per month period (rolling)
- A Website Design Contract → 1 Project

Each Project's `Source Contract Record ID` points back at the same Contract `recXXX...`. Cross-base join in BigQuery: `Operations.projects.source_contract_record_id = CRM.contracts._airtable_record_id`.

## Service Catalog ↔ CRM alignment

The CRM base's `Contracts.Services` multi-select is the user-facing list of what's been sold. Service Catalog rows in Operations are the canonical service definitions.

**Discipline:** when adding a Service Catalog row, manually update CRM `Contracts.Services` multi-select options to match the Service Name. When sunsetting (Active = false in Service Catalog), keep the option in the CRM multi-select for historical contracts but flag in the operational dashboard as "Service in catalog: inactive."

The Brain should produce a weekly drift report comparing the two — that's a follow-up enhancement (deferred from v1).

## "Drive Files" handling

Spec §3.1 references `Linked record (Drive Files)` for project documents. v1 models this as a `url` field (`Projects.Scope Document`) pointing at the Drive file directly — there is **no separate Airtable Documents table**. Drive metadata is registered through Knowledge Catalog (spec §10.1) instead. Revisit if the Drive↔Airtable round-trip becomes operationally painful.
