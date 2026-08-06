# ADR 0022 — Triage Agent no-match policy and Owner-id sync

**Status:** Accepted
**Date:** 2026-04-29
**Workstream:** WS-G1 (Triage Agent) PR 4g
**Related:** ADR 0011 (two-base architecture), ADR 0021 (CRM read-sync), PRD §6.3 + §4.7

## Context

PR 4g (this work) wires `airtable_replica.sender_to_project_v` (built in ADR 0021) into the Triage Agent so a classified Gmail signal lands as a drafted Airtable Task under the right Project. The view's hit rate is bounded by CRM Contact population — 1 fully-populated Contact today — so the **no-match path is run-of-the-mill, not exceptional**. We're treating it as a first-class behavior.

This ADR locks down three decisions that aren't otherwise derivable from PRD or earlier ADRs:

1. What happens when the resolver returns no match.
2. How domain-only matches behave (HIPAA-safety constraint).
3. How the agent populates `Tasks.Owner` (required `singleCollaborator` field) when drafting.

## Decision 1 — No-match policy: Triage Inbox + audit + BQ trail (most-complete)

When the resolver returns None for a Gmail signal that the LLM classified with `confidence >= 0.7` and `action_type != do_now`:

- **Draft to a sentinel "Triage Inbox" Project** in Operations, with `Owner` left unset (Airtable inherits from `Project.Owner` if so configured, otherwise the field is empty for the operator to fill on approval). The Project is created **manually** in Airtable (drafts-only boundary, PRD §4.7); the agent never auto-creates Projects.
- **Pin the sentinel's record id** in env var `TB_TRIAGE_INBOX_PROJECT_ID`, threaded through `terraform.tfvars` → Reasoning Engine env vars (set in `scripts/deploy_triage_re.py`).
- **Emit a `triage.no_project_match` log event** (structured JSON via `logging.info`) so monitoring tooling can extract rates over time. Cloud Logging picks the JSON payload up automatically.
- **The BQ row in `agent_outputs.triaged_items`** carries `airtable_task_record_id` of the Inbox draft. So the BQ trail records both that the signal was drafted (link present) AND that it landed in the sentinel project (the linked Airtable row's `Project` field equals the inbox id).

Three surfaces — Airtable (visibility for the operator), structured log (monitoring), BQ (analytics + the canonical record). Belt + suspenders + parachute. Setup cost is one Airtable Project row + one env var.

### Why not skip-and-log only?

A leaner alternative was considered: write the BQ row with `airtable_task_record_id IS NULL` and rely on a saved `bq query` to surface no-match items. Lighter infra, no Airtable noise. But it requires the operator to remember to run the query, and the Triage Agent's value proposition is "things land where you'd already be looking." Triage Inbox preserves that, at the cost of one manual Airtable row.

### Why not auto-create per-client unrouted projects?

Rejected — would require the agent to create Projects, violating PRD §4.7's drafts-only boundary. The agent's only Airtable writes are to Tasks (with `Source = "Triage Agent"`, `Approval Status = "Drafted by Agent"`).

## Decision 2 — Domain match: same-client guard, free-mail skip

The resolver tries exact email match first. If that misses, domain match is allowed **only when every candidate Active project at that domain belongs to the same `client_id`**. Otherwise the resolver returns None.

Why: the materialized view already filters HIPAA clients out at the source (PRD §4.1 layer 2), but a HIPAA client and a non-HIPAA client could share an email domain (`example.com` for two unrelated companies; an agency partner servicing multiple clients). Without the same-client guard, domain match could route a HIPAA-client signal to a different client's project — silent boundary leak.

Free-mail domains (`gmail.com`, `outlook.com`, `yahoo.com`, `icloud.com`, `proton.me`, etc.) are skipped from the domain pool entirely. A match against `gmail.com` would route every personal Gmail of every contact to whatever single Active project happened to share the domain — almost certainly wrong. Exact match on a free-mail address (e.g. `owner@gmail.com` is in the CRM as someone's contact) still works because exact match is high-trust.

### Implications

The day-1 hit rate is even lower than a permissive resolver would yield, but the resolver is **HIPAA-safe** and the cost (more no-match drafts → more Inbox traffic) is fine: the Inbox path is a first-class behavior, not an exception.

## Decision 3 — Sync Team `usrXXX` ids and set Owner explicitly

Airtable's `singleCollaborator` field accepts the bare `usrXXX` user id on POST. It does *not* accept `{"email": "..."}` for write operations (verified during planning). So to set `Tasks.Owner` correctly when drafting, the agent must have the `usrXXX` for whoever owns the resolved Project.

Implementation:

1. **Manual Airtable change**: add a `User` field of type `singleCollaborator` to Operations.Team. the operator sets it once per row.
2. **Schema annotation**: `airtable/schema.json` declares the new field with `_extract: "user_id"`. Default sync behavior for `singleCollaborator` is to extract `email`; the annotation flips this one field to extract `id` instead.
3. **Sync change**: `_translate_value` honors `_extract`. Default unchanged (back-compat for every other singleCollaborator field — `Projects.Owner`, etc., still extract email).
4. **View change**: `sender_to_project_v` `LEFT JOIN`s `airtable_replica.team` on `LOWER(team.workspace_email) = LOWER(p.owner)` and surfaces `team.user AS owner_user_id`. LEFT so projects whose owner has no Team row don't drop out — `owner_user_id` is just NULL.
5. **TaskDrafter**: `owner_user_id` (renamed from `owner_record_id` for clarity) is passed verbatim as `fields["Owner"]`.

### Why not `{"id": "usrXXX"}` or Airtable Users API at runtime?

- The bare string form is what Airtable's `singleCollaborator` field accepts on POST; the dict form `{"id": "..."}` is the *response* shape, not the write shape. Tested in PR1.
- Calling Airtable's Users API at agent runtime (to translate `email → usrXXX`) adds a hot-path API round trip + a separate scope on the PAT. The Team-table sync approach amortizes the lookup over the existing 15-min sync cycle.

### Why not a hand-curated dict in code?

For 2-3 team members it would work. But it scales worse than syncing through Airtable, and the sync path makes the data source visible in BQ for ad-hoc queries.

## Constructor invariant in TriageAgent

If `task_drafter` is set, `items_writer` AND `project_resolver` AND `inbox_project_id` must all be set. The constructor raises `ValueError` otherwise — programmer error caught at instance construction, not runtime. Without `items_writer`, drafts would land in Airtable with no BQ link-back. Without `project_resolver` or `inbox_project_id`, drafts would have nowhere to land on no-match. Both are silent-data-loss bugs the invariant prevents.

## Order of operations in `_run()`

Airtable POST happens **before** the BQ row write. This avoids the streaming-buffer problem on post-write updates (BQ blocks updates to a row for ~90 minutes after streaming insert). The Airtable record id is captured on the way in and threaded into the BQ row's `airtable_task_record_id` column on the single insert.

If the Airtable POST raises, `_maybe_draft_airtable` logs and returns None — the BQ row still gets written with `airtable_task_record_id = NULL`. The "BQ remains audit trail" contract is locked down by `test_airtable_draft_failure_still_writes_bq_row`.

## Consequences

- One new manual Airtable column (`Team.User`).
- One new manual Airtable Project (`Triage Inbox`).
- One new Secret Manager secret (`airtable-tasks-write-pat-prod`).
- One new IAM binding (existing `asb-agent-triage-sa` → secret accessor on the new secret).
- Three new env vars on the deployed Reasoning Engine (`TB_TRIAGE_INBOX_PROJECT_ID`, `AIRTABLE_OPS_BASE_ID`, `AIRTABLE_TASKS_WRITE_PAT_SECRET_ID`).
- Materialized view `sender_to_project_v` re-created (TF will manage this — destruction protection is on but a destroy + create is intentional given the SQL change).
- A small bump in resolver hit rate when a domain has a single Active client behind it (currently rare, but the agency-partner case will fire as Contacts populate).

## Revisit if

- The Airtable Users API exposes `usrXXX` lookup by Workspace email cheaply (would let us drop the Team.User field).
- A HIPAA flag is added to the CRM directly (would change the same-client-guard analysis).
- the operator asks for finer no-match grouping than the single Inbox project (e.g. one Inbox per Account Manager).
- The free-mail domain list grows unwieldy — at which point we'd switch from a hard-coded set to an Airtable Risk Profiles-style config row.
