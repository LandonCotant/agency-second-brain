# ADR 0011 — Airtable Operations base (separate from Sales/CRM base)

**Status:** Superseded by ADR 0020
**Date:** 2026-04-27
**Workstream:** WS-B (Data Pipeline)
**Related:** PRD §6.2, ADR 0010 (Cloud Run Job for sync — host model unchanged), ADR 0020 (collapses the two bases into one)

## Context

The original `airtable/schema.json` (PR #1 + PR #6) was built from spec §3.1's idealized seven-table operational base. The user already operates a `Sales & CRM` Airtable base (`appj1Bo8Uw6Oa6MwE`) with four tables: Accounts, Contacts, Leads/Opportunities, Contracts. None of the operational tables the spec assumed — Tasks, Projects, Goals, Goal Scores, Team, Risk Profiles — exist anywhere yet, because they had no project-management base.

The user is intentionally transitioning to Airtable as the **operational center of the agency**. Doing it halfway leads to schema cleanup work in 3-6 months, so this ADR makes design choices that target a 2-3 year operating horizon, not v1 minimum.

## Decision summary

1. **Two bases.** Sales/CRM stays as-is. A new **Operations** base owns delivery: Projects, Tasks, Goals, Goal Scores, Team, Risk Profiles, Service Catalog, plus a thin Clients reference.
2. **Projects spawn from Contracts.** Each active Contract spawns 1+ Projects in Operations. Cross-base linkage is by record-ID string (`Projects.Source Contract Record ID` stores the CRM Contract `recXXX...`).
3. **Service Catalog is a first-class table** in Operations. `Projects.Project Type` is a linked record to Service Catalog (not a single-select). Adding/sunsetting a service = one row, no schema migration.
4. **Phase model.** `Projects.Phase` is a single-select (Discovery / Design / Build / Launch / Maintain / Closeout). Milestones table deferred until cross-phase reporting is needed.
5. **Time tracking deferred.** Optional Time table linked to Tasks lands when billing model stabilizes.
6. **Recurring tasks manual.** No Airtable Automations for templated task creation in v1.
7. **Team is a table, not just user fields.** Captures roles, capacity, off-platform metadata. Workspace email is the join key for Brain ownership resolution.
8. **Goals + Goal Scores live in Airtable, not Memory Bank.** Humans edit goals weekly; Memory Bank is wrong for that.
9. **Brain writes drafts, humans approve.** Tasks created by Triage / Risk Watcher carry `Source` and `Approval Status = Drafted by Agent`. Aligns with PRD §4.7 "drafts only" boundary; the human queue is the source of truth.

## Operations base — 8 tables

### Core operational

1. **Clients** — thin reference; mirrors CRM Accounts by ID. Operational view (status, segment, AM, denormalized for query speed). Humans edit operations-specific fields here; CRM remains canonical for sales fields (lifetime value, lead source, etc.).
2. **Projects** — units of delivery. Linked to Clients + Service Catalog + (soft) Source Contract. Phase + Health + Owner + dates. Source Contract is by record-ID string because cross-base linked records aren't supported.
3. **Tasks** — the operational task list. Brain's primary write target (drafts). Linked to Projects (required) + Goals (optional) + Owner (required). Source field distinguishes Manual / Triage Agent / Risk Watcher / Goal Steward / Other. Approval Status routes Brain drafts through human review.
4. **Goals** — strategic hierarchy (10y / 3y / 1y / Quarterly / Weekly). Parent Goal links upward; horizon-rule invariant enforced by Goal Steward.
5. **Goal Scores** — weekly self-assessment (1-10) per goal. Goal Steward agent collects via Chat prompt and writes here.
6. **Team** — team members with role + capacity + Slack handle. Workspace email is the join key Brain uses for ownership resolution.

### Configuration / reference

7. **Service Catalog** — single source of truth for service offerings. Six v1 rows: Vantage Local Platform · Vantage Enterprise Platform · Local SEO Lead Gen · Website Design · Paid Ads Setup · Paid Ads Management · Marketing Consulting Retainer. Each row carries Category (Platform Subscription / Project-Based / Retainer), default phase sequence, default deliverables, typical engagement length.
8. **Risk Profiles** — per-segment trouble-signal config Risk Watcher reads. Segment + Pattern Name + Threshold + Severity Default + Active.

## Why two bases over one

- **Sales hygiene and operational hygiene have different cadences.** Lead pipeline reviewed weekly by sales lens; project health reviewed daily by ops lens. Mixing them means views and automations get tangled fast.
- **Independent evolution.** The user expects to make additional bases later (likely client-facing portals, internal wikis). Two-base discipline establishes the pattern early.
- **Access scoping.** Future contractors might need ops access without seeing the sales pipeline or vice versa. Easier with separate bases.
- **Cost of separation:** cross-base linked records aren't supported. Mitigated by storing CRM record IDs as text fields and joining at query time. The Brain handles this in BigQuery (CRM and Operations both replicate to `airtable_replica.*`; joins happen in BQ).

## Why Service Catalog as a table, not single-select

- **Single source of truth.** Adding a new service = one row in Service Catalog. The select-list-on-Projects-and-Contracts approach diverges fast (we already saw this — your CRM Contracts.Services multi-select would need to be edited every time, and stays stale).
- **Carries config.** Default phase sequence, default deliverables, pricing notes — these belong somewhere. A linked-record table is the right home; a single-select can't hold them.
- **Caveat:** CRM Contracts.Services stays a multi-select (cross-base linked records aren't supported). Human discipline keeps the option list aligned with Service Catalog row names. the operator (or a Brain follow-up agent) periodically reconciles.

## Why Projects spawn from Contracts (not Accounts)

- **Billing traceability.** User bills by Contract. Project-to-Contract linkage means revenue attribution and project ROI are queryable.
- **Cross-base storage.** Cross-base linked records unsupported, so `Projects.Source Contract Record ID` is a text field holding the CRM Contract `recXXX...`. The Brain (and humans) join at query time.
- **A Contract can spawn multiple Projects.** A retainer Contract that bundles SEO + Paid Ads + Consulting Retainer = three Projects. Each Project links back to the same Contract ID.

## Brain integration impact

- **Triage Agent** writes Tasks with `Source = "Triage Agent"`, `Approval Status = "Drafted by Agent"`, Owner = Project Owner. Human approves in Airtable.
- **Risk Watcher** same pattern: writes Tasks with `Source = "Risk Watcher"`, drafts. Underlying signal evidence still goes to BQ `agent_outputs.risk_flags`.
- **Goal Steward** reads/writes Goals.Status + Last Reviewed; writes Goal Scores rows weekly via Chat prompts.
- **Morning Brief** reads Projects (active engagements), Tasks (today's queue), Goals (context). Drafts to Gmail. No writes.
- **HIPAA story.** No HIPAA data in this base today. The `Clients.HIPAA` checkbox stays as a load-bearing field with `false` on every row, so the cascade machinery is wired and inert. If a HIPAA client is ever onboarded, flip the flag and the Brain stops ingesting them within one sync cycle.

## What was rejected

- **One base for everything.** Cleaner-looking initially but loses the ops/sales cadence separation and locks future scoping into a single permission model.
- **Service Catalog as Project Type single-select on Projects.** Stale option lists become a maintenance liability.
- **Projects spawn from Accounts (not Contracts).** Loses billing traceability; harder to attribute project ROI to specific deals.
- **Goals in Memory Bank instead of Airtable.** Memory Bank isn't human-editable; Goal Steward editing via Chat prompts only would be unusable.
- **Brain auto-creates approved Tasks (no draft review).** Violates PRD §4.7 drafts-only boundary; we'd lose the human-in-the-loop guard against bad classifications.
- **Templated task automation in v1.** Premature. Adding it after schema stabilizes is mechanical; adding it now risks tying us to a wrong template shape.

## Consequences

- **The previously merged airtable_replica.* BQ schema (PR #6) gets rebuilt** with the new table set. Tables are empty (sync never ran), so destroy + recreate is safe — no data loss.
- **The merged sync code (PR #6, `airtable_to_bq.py`) gets its field mappings rewritten** to match the new schema. Cloud Run Job + Scheduler + IAM + custom role all stay; they're schema-agnostic infrastructure.
- **A new manual setup runbook (`docs/runbooks/airtable_operations_base_setup.md`)** walks the user through building the Operations base in Airtable, including the Service Catalog seed rows.
- **The `airtable_base_id` Terraform variable** (currently empty default) is set by the user once they create the Operations base.
- **Two PATs eventually** — one for the Operations base (the sync uses this), one for the CRM base (the Brain reads CRM live for sales context when needed; PAT not wired in v1). v1 ships with only the Operations PAT.

## Revisit if

- The user creates a third base and the cross-base join story becomes painful.
- Goal Steward needs lookups against agent_outputs.goals more often than weekly — at which point Memory Bank's read characteristics may beat Airtable.
- Project Templates becomes a real ask (when the user has built the same Website Design project 3+ times manually).
- Time tracking becomes essential to billing.
