> **Portfolio copy.** Identifiers are placeholders. See [PORTFOLIO.md](PORTFOLIO.md).
> In docs, prefer “sensitive / regulated-account isolation” over HIPAA branding;
> some code symbols still use `hipaa_*` for cascade/test compatibility.

# Agency Second Brain — Orientation

You are Claude Code working on the Agency Second Brain build. This file is a map. Detail lives in the docs it points to.

## What this build is

Multi-agent system on GCP Agent Platform that triages inbound work, watches client risk signals, and drafts daily briefs for a 2-person digital marketing agency (the agency). The user is the implementer (`owner@example.com`). "the operator" in some legacy docs is the same person — single-founder for now.

**Source of truth, in order of precedence:**
1. **`PRD.md`** — implementation PRD (mandates tech stack, security, milestones)
2. **`docs/source/proactive_agency_brain_spec.md`** — design spec (defers to PRD on conflicts)
3. **`docs/source/agency_goal_hierarchy_v0.1.md`** — strategic context

## Where to find detail

- **Live deploy state** → `docs/PRODUCTION_STATE.md` (check the `Last verified:` date stamp; if stale, prefer `gcloud`/`bq`/`gh` over the snapshot)
- **Decisions ledger (44 ADRs)** → `docs/adr/INDEX.md`
- **Current focus + housekeeping** → `docs/ROADMAP.md`
- **Operational procedures** → `docs/runbooks/`
- **Module acceptance specs** → `docs/acceptance/`
- **Strategic specs** → `docs/source/`
- **DWD scope source of truth** → `docs/dwd_scopes.md`

## Decisions you must NOT re-litigate

These are the load-bearing invariants. Read the linked ADR before pushing back; if you disagree, write a new ADR superseding it. Everything else → `docs/adr/INDEX.md`.

- **[ADR 0013](docs/adr/0013-unify-bq-dataset-locations.md)** — All BQ datasets at `US` multi-region. Cross-dataset JOINs depend on it.
- **[ADR 0017](docs/adr/0017-model-armor-disabled-at-runtime.md)** — Model Armor disabled at runtime; drafts-only carries the residual prompt-injection risk.
- **[ADR 0024](docs/adr/0024-cost-guardrails.md)** — Cost guardrails: AR cleanup, BQ partition TTLs, $50/mo budget alert.
- **[ADR 0027](docs/adr/0027-dwd-delegation-surface.md)** — DWD allowlist: one SA (`asb-agent-triage-sa`), two scopes (`gmail.compose` + `calendar.readonly`). Other agents *impersonate* this SA — they don't get DWD themselves.
- **[ADR 0028](docs/adr/0028-reasoning-engine-create-alert.md)** — No new Reasoning Engines (orphan-RE cost incident burned $40/day). Use Vertex SDK direct.
- **[ADR 0037](docs/adr/0037-pkm-merge-architecture.md)** — PKM uses single `agent_outputs.*` dataset with a `scope` column, not separate datasets.
- **[ADR 0038](docs/adr/0038-embeddings-and-vector-search.md)** — No managed Vertex Vector Search index; use BQ `VECTOR_SEARCH` table function ($30+/mo idle blocks the $50/mo budget).
- **[ADR 0044](docs/adr/0044-drive-write-via-folder-share.md)** — Drive write via folder-share + ADC, NOT DWD impersonation. Preserves the 0027 §2 invariant.
- **[ADR 0045](docs/adr/0045-librarian-as-ingestor-and-multi-root.md)** — Librarian writes to BOTH Brain + Solutions Shared Drives; copy-fallback on cross-Drive 403; archive-on-copy + audit skip-list to prevent duplicates.

## User preferences

From `~/.claude/projects/<local>/memory/`:

- **Pragmatic security over PRD-prescribed defense-in-depth** when marginal cost is real and marginal risk is low for this 2-person tool. Cite cost when proposing security infrastructure beyond defaults. Always write an ADR when deviating.
- **Production-touching workflow:** branch + edit + ADR → `terraform apply -target=<resources>` (preferred discipline) → pause for explicit approval before any destructive op (`bq rm`, `terraform destroy`) → PR is the audit trail, not a gate.

## How to interact with the user

- **Plan before implementing.** They expect a structured plan + decision points before code lands. Use `ExitPlanMode` for non-trivial work.
- **They'll push back on overengineering.** "Don't do things halfway, but don't do enterprise ceremony for a 2-person tool" is the consistent signal.
- **For destructive operations**, ask explicitly. They've been burned by side effects (Compute SA auto-creation, partial bases, etc.) — show the diff before applying.
- **Prefer worktrees + PRs** for substantial changes. Direct commits to main are OK for hotfixes / docs but not for new features.
- **No emojis in files** unless explicitly asked.

## Files you'll likely touch first in any session

- `PRD.md` — full implementation PRD
- `docs/adr/` — every meaningful decision (start at `INDEX.md`)
- `docs/runbooks/` — operational procedures
- `airtable/schema.json` — source of truth for the Operations base shape. **Operations base ID: `appXXXXXXXXXXXXXX`.** Pass table + field *names* (not IDs) to the Airtable MCP — names are resolved case-insensitively. Skip `list_tables_for_base` (12K+ tokens of wasted discovery); only call `get_table_schema` when you need choice IDs to filter on a singleSelect.
- `terraform/envs/prod/main.tf` — env-level wiring; modules at `terraform/modules/{foundation,agent_runtime,data_pipeline,observability,security}/`
- `src/agency_brain/` — Python source: `common/`, `agents/{base.py,triage/}`, `routing/`, `sync/`, `audit/`

## Codebase gotchas (lessons that only surface at runtime)

These cost ~30 minutes and three PRs (#82, #83) on 2026-05-04 because deploy-then-discover is the only path that exercises them. Check them BEFORE writing code that exercises a new column or import.

- **Airtable `singleCollaborator` columns materialize as the user's email, not the Airtable user_id.** The sync extracts `email` by default (`sync/airtable_to_bq.py:154`); only `Team.User` opts into `user_id` via the `_extract: user_id` annotation per ADR 0019. Don't write code that JOINs `accounts.account_manager` against `team.user` — that's "user_id IN (email)" and silently returns no rows. **Before touching a singleCollaborator/multipleCollaborators column: `bq query 'SELECT <col> FROM airtable_replica.<table> LIMIT 5'` to confirm the column shape.**
- **Per-agent Dockerfiles pin their own deps explicitly; new imports require a Dockerfile change.** `Dockerfile.{risk-watcher,morning-brief,notes-ingestor,routing-fanout,triage-bridge,airtable-sync,audit}` each list a minimal `pip install` set. Adding `from googleapiclient...` to an agent that didn't previously do Calendar/Gmail will fail at runtime with `ModuleNotFoundError`, NOT at build time. **Before adding an import that crosses a new dep boundary: grep the agent's Dockerfile against the import.** Reference pin set: `Dockerfile.morning-brief` (Calendar v3 + Vertex), `Dockerfile.routing-fanout` (Gmail + Chat webhook).
- **Image-deps interact with code-paths that fire conditionally.** A code path gated on a runtime condition (e.g. `owner_email is not None` for the Calendar call) won't surface a missing dep until that condition first holds in prod. Pre-merge smoke-fire any new code path against real data — local pytest isn't enough.
- **`airtable/schema.json` is bundled into the sync image at build time** (`Dockerfile:23`). Schema edits MUST be paired with an `airtable-sync` image rebuild + Cloud Run Job rollout (`cloudbuild.airtable-sync.yaml`) — otherwise the next sync's WRITE_TRUNCATE strips the new columns.
- **`VECTOR_SEARCH` requires an `ARRAY_LENGTH(embedding) = 768` pre-filter** (ADR 0045 §9). One pre-ADR-0038 row in the corpus has length-0 embedding which crashes the function unless callers pre-filter via subquery.
- **`gcloud run jobs execute --update-env-vars=K=V` REPLACES the env block via containerOverrides — it does NOT merge.** Required env vars set on the Job spec (e.g. `CRM_UPDATER_INBOX_PROJECT_RECORD_ID`) disappear for that one execution and the container fails on `_env(required=True)`. Surfaced 2026-05-11 during CRM Auto-updater DWD smoke. **For operator-fires that need a custom env, do `gcloud run jobs update --update-env-vars=K=V` first, then `execute` plain, then revert.** Scheduler-side containerOverrides (e.g., Evening Reflection's REFLECTION_MODE per PR #117) ARE safe because they pre-grant `run.jobs.runWithOverrides` AND the scheduler payload contains only the keys it wants to override (other keys still resolve from the Job spec). The replace-not-merge behavior bites the operator-side `execute` flag.

## Safety rails

- **HIPAA isolation** (PRD §4.1) is the load-bearing guarantee. Don't touch the cascade machinery (Lookup fields on Projects/Tasks, filterByFormula in `hipaa_filters.py`, the alert wiring) without thinking through the full chain.
- **Drafts only** (PRD §4.7). No agent has Gmail-send / Airtable-write-to-source-tables capability. The only Airtable writes the Brain does are to its own output tables AND to Tasks with `Approval Status = "Drafted by Agent"` (humans approve). With Model Armor runtime disabled (ADR 0017), drafts-only is doing more of the prompt-injection mitigation than originally planned — don't loosen it without re-litigating ADR 0017.
- **No service accounts with predefined high-privilege roles.** `scripts/least_privilege_check.py` enforces this on every PR. Don't bypass.

## When you finish a deploy

Update `docs/PRODUCTION_STATE.md` — bump the `Last verified:` date, edit the affected module's row in the deployment table, and update the schedulers table if you added or removed a cron. Don't re-bloat this file with state.
