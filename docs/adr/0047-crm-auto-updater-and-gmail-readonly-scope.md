# ADR 0047 — CRM Auto-updater + Gmail readonly + modify scopes

**Status:** Accepted
**Date:** 2026-05-09
**Workstream:** WS-G CRM Auto-updater (parallel with WS-G7 Knowledge Surfacer)

**Supersedes (in part):** ADR 0027 §2 (DWD scope list).

## Context

the operator maintains a Gmail filter that applies the `secondbrain` label to
emails he considers relevant to his agency operations and personal
network. This is a high-quality, human-curated relevance signal —
strictly better than any classifier the Brain could build, because the
user is the ground truth for "matters."

Today, that label is a dead-end: nothing reads it. The user manually
extracts task items, contact updates, and account context from those
emails into Airtable. This is the highest-friction work he has left
that the Brain hasn't yet automated.

The CRM Auto-updater consumes the `secondbrain` label as input and
produces *drafted* Airtable updates as output: new Tasks (existing
pattern, ADR 0019), Contact updates, Account updates. Drafts-only per
PRD §4.7 — humans approve every change.

This ADR documents (a) the agent itself, (b) the DWD scope expansion
required to read labeled email bodies, (c) the threat model around
that expansion.

## Decisions

### 1. Trigger: daily Cloud Run Job at 06:15 PT

Polling cadence; sub-day latency is acceptable for the Auto-updater
because the operator's review-and-approve loop is itself daily. Runs before
the Morning Brief (07:25 PT) so newly-drafted Tasks are visible in
that day's brief.

Rejected: Pub/Sub-triggered immediate processing on label apply.
Reason: requires Workspace push notifications (Gmail watch + Pub/Sub
+ topic subscription) which adds infrastructure with no UX win at
~5–15 labeled emails/day.

### 2. Source scope: Gmail messages with `secondbrain` label, since
last successful run

Each run pulls messages whose `historyId` is greater than the previous
run's recorded checkpoint, filtered to the `secondbrain` label. The
checkpoint lives in `agent_outputs.crm_updater_runs` (new table; ADR
0009 schema family).

Idempotency: messages are deduplicated by Gmail message_id before
extraction. After successful draft creation, the agent applies a
`secondbrain-processed` label so subsequent runs skip them. Failure
to apply the dedup label is logged but not fatal — re-processing
creates duplicate drafts which the human approves once and dismisses
the rest.

### 3. DWD scope expansion: `gmail.readonly` + `gmail.modify` on
`asb-agent-triage-sa`

ADR 0027 §2 set the DWD allowlist at `{gmail.compose,
calendar.readonly}`. The Auto-updater needs:

- `gmail.readonly` to read message bodies of `secondbrain`-labeled
  threads.
- `gmail.modify` to apply the `secondbrain-processed` label after
  drafting (label-only; combined with the static check below it
  cannot send messages or modify content).

Both scopes attach to **`asb-agent-triage-sa` only** — the existing
DWD-grantable SA per ADR 0027 §3. The Auto-updater's runtime SA
(`asb-crm-updater-sa`) impersonates this SA. ADR 0027 §3 invariant
preserved.

The new scopes are added to the DWD allowlist via the Workspace Admin
Console (manual one-time step, documented in
`docs/runbooks/dwd_scope_expansion_2026-05.md`). The PR-gate static
check at `scripts/drafts_static_check.py` is updated to permit
`users.history.list`, `users.messages.list`, `users.messages.get`,
`users.labels.list`, and `users.messages.modify` for label-apply only
— it continues to forbid `users.messages.send`.

### 4. Auto-updater SA: new `asb-crm-updater-sa`, NOT reuse
`asb-agent-triage-sa`

ADR 0027 §3 invariant: only one DWD-grantable SA. The Auto-updater
runtime is a NEW SA (`asb-crm-updater-sa`) that holds:

- `iam.serviceAccountTokenCreator` on `asb-agent-triage-sa` (impersonates
  for Gmail read + label apply).
- `bigquery.dataViewer` on `airtable_replica` (HIPAA domain list, account
  name list).
- `bigquery.dataEditor` on `agent_outputs` (`crm_updater_runs` checkpoint
  table) and `agent_audit_log` (BaseAgent contract).
- `secretmanager.versions.access` on the existing Airtable PAT secret.
- `aiplatform.endpoints.predict` (Vertex Flash for extraction).
- Custom role only — no predefined high-privilege roles
  (`scripts/least_privilege_check.py` enforces).

### 5. Output: drafted Airtable rows, drafts-only

Three writers:

- **Tasks**: `Approval Status = "Drafted by Agent"` (existing pattern
  per ADR 0019). Optional `Owner`, `Linked Contact`, `Linked Account`
  populated when extractor identifies them.
- **Contacts**: `Pending Updates` long-text field appended with a
  timestamped block:
  ```
  [2026-05-09 from gmail-msg-{id}] Suggested Last Contact: 2026-05-08
                                   Suggested Next Followup: 2026-05-22
                                   Suggested Warmth: Warm
                                   Source: Re: Q2 review thread
  ```
  Human reads, decides what to commit, deletes the block. v1.5 may
  promote individual fields to a sibling `Pending Contact Updates`
  table with explicit approval status.
- **Accounts**: `Pending Updates` long-text field appended similarly.
  Surfaces new contacts to add, mention context, suggested status
  changes.

Rejected for v1: writing directly to `Last Contact` / `Next Followup`
/ `Warmth` fields. Reason: drafts-only invariant (PRD §4.7) — those
fields are user-canonical, not agent-drafted-then-approved. The
long-text staging area preserves the boundary.

### 6. Extraction: `gemini-2.5-flash` with structured output

Mirrors the rest of the fleet (ADR 0028, 0029, 0033, 0040). Strict
`response_schema` for tasks, contact updates, account mentions.
System prompt: "Only extract entities explicitly mentioned in the
email; do not infer; if uncertain, skip."

No Pro escalation for v1 — the volume is low and the human review
catches misses.

### 7. HIPAA pre-flight: reject any email whose sender or recipients
include a HIPAA-flagged domain

Defense in depth (PRD §4.1 layer 3). HIPAA domains are derived from
`airtable_replica.accounts WHERE hipaa = true` at run start (cached
per run). Any rejected email is audited as
`HIPAA_GUARD_TRIPPED`; the dedup label is NOT applied so re-runs
re-evaluate (i.e., we don't quietly skip — the audit row is the
explicit record).

Today's HIPAA account count is 0 — this guard is forward-defense.

## Cost estimate

At ~10 labeled emails/day, ~3K tokens input + 500 tokens output per
extraction:

- Gemini Flash: 10 × ($0.30 × 3K + $2.50 × 0.5K) / 1M = ~$0.04/day = ~$1.20/mo
- Cloud Run Job (single invocation/day, ~30s runtime): ~$0.05/mo
- Gmail API + BQ jobs: free / negligible
- **Total: ~$1.25/mo**

Within the $50/mo envelope (ADR 0024).

## Threat model — DWD `gmail.readonly` + `gmail.modify` expansion

This is the load-bearing security change. Today, `asb-agent-triage-sa`
holds `gmail.compose` (drafts only — cannot read inbox content) +
`calendar.readonly`. After this ADR, it additionally holds
`gmail.readonly` + `gmail.modify`.

The threat shape:

1. **Compromised triage SA → can read all inbox content.** Today
   compromise yields draft-create-only, which a human review catches.
   After the expansion, compromise yields full inbox read + label
   modification on the operator's mailbox. Mitigations:
   - Custom role on `asb-crm-updater-sa` is the ONLY identity that can
     impersonate `asb-agent-triage-sa` for these new scopes (the
     impersonation grant is scope-limited via `target_scopes=[scope]`
     in `common/dwd.py`).
   - Cloud Run Job + private endpoint — the only invokers are
     `asb-crm-updater-invoker@` (the Cloud Scheduler invoker) and
     break-glass human invocation via `gcloud run jobs execute`.
   - Audit log (`agent_audit_log.events`) records every invocation with
     source SA + impersonation chain + email count.
2. **`gmail.modify` opens label send-via-modify-state vector.** The
   API permission allows changing message labels, but NOT sending
   messages or modifying body. The PR-gate static check
   (`scripts/drafts_static_check.py`) forbids `users.messages.send`,
   `users.messages.trash`, `users.messages.batchModify` in src/.
   `users.messages.modify` is permitted but only for label-apply (no
   `removeLabelIds` other than `secondbrain` — not `INBOX`,
   `IMPORTANT`).
3. **Label-scoped read leaks beyond `secondbrain`.** The `gmail.readonly`
   scope is mailbox-wide, NOT label-scoped — Google's API doesn't
   support per-label scoping. The application layer enforces "only
   process messages with the `secondbrain` label" via the API query
   parameter (`q=label:secondbrain`). A bug that drops the filter
   would silently process all inbox messages.
   - Mitigation: unit test `test_gmail_client.py::test_list_query_includes_label`
     asserts the query string contains `label:secondbrain`.
   - Mitigation: HIPAA pre-flight always runs, regardless of label — a
     wrongly-included HIPAA email would be rejected at extraction time.

The residual risk is "an attacker who compromises the operator's GCP project +
Cloud Run + the `asb-crm-updater-sa` service account can read his inbox
and apply labels." This is a strict subset of "an attacker who
compromises the project can already do everything the Brain does"
(read all BQ, draft all Airtable rows, write all `agent_outputs`).
The expansion is reasonable for the productivity gain.

## Operational notes

- The DWD scope additions are a Workspace-level config change — they
  affect how the Workspace tenant authorizes the SA. ADR 0027's
  daily IAM drift audit (`audit/hipaa_iam_drift.py`) plus the
  drafts boundary audit (`audit/drafts_boundary_check.py`) cover the
  ongoing posture; the runbook
  `docs/runbooks/dwd_scope_expansion_2026-05.md` documents the
  one-time apply.
- `airtable/schema.json` adds the `Pending Updates` long-text field
  to both Contacts and Accounts. Per the CLAUDE.md gotcha, this
  triggers an `airtable-sync` image rebuild + Cloud Run Job rollout
  (PR B2). Without the rebuild, the next sync's WRITE_TRUNCATE strips
  the new columns from BigQuery.
- `agent_outputs.crm_updater_runs` is a new BQ table (4 cols + audit
  bookkeeping). Schema in PR B2 Terraform.

## Consequences

**Positive:**
- Removes the highest-friction manual workflow the operator still does daily.
- The `secondbrain` label is reused as the relevance gate — no
  classifier work, no false-positive tuning.
- Drafts-only stays load-bearing; humans still approve every change.
- `asb-agent-triage-sa` remains the only DWD-grantable SA (ADR 0027 §3).

**Negative (accepted):**
- DWD allowlist grows from 2 scopes to 4. Threat model expands as
  documented above; mitigations are in place.
- New BQ table (`crm_updater_runs`) for run-checkpoint state.
- `airtable/schema.json` change requires the airtable-sync image
  rebuild gotcha to be respected.
- `Pending Updates` long-text field is a crude review surface for v1.
  v1.5 should consider a sibling `Pending Contact Updates` table with
  explicit approval-status singleSelect for cleaner UX.
