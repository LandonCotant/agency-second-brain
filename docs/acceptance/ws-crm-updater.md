# CRM Auto-updater — Acceptance Criteria

Sign-off gate for the CRM Auto-updater (ADR 0047). PR B1 (module +
tests + ADR) → PR B2 (Terraform + DWD scope expansion runbook) →
PR B3 (this acceptance + operational runbook + PRODUCTION_STATE
bump).

**Depends on:** WS-A (foundation), WS-G1 (Triage Agent — shares the
Airtable Tasks "Drafted by Agent" pattern), ADR 0042 (Personal CRM
schema), ADR 0027 (DWD discipline).

## Functional checks

- [ ] **Labeled email → Airtable drafts.** Apply the `secondbrain`
  label to a known-test email; force-fire `asb-crm-updater` via
  `gcloud run jobs execute --update-env-vars=CRM_UPDATER_MAX_MESSAGES_PER_RUN=1 --wait`.
  Verify:
  - A Task draft appears in Airtable Tasks with
    `Approval Status = "Drafted by Agent"`.
  - The relevant Contact row's `Pending Updates` field has a new
    timestamped block (date, gmail-msg-id, suggested updates).
  - The Account row's `Pending Updates` field also has a new block
    (if the email surfaced an account mention).
  - The `secondbrain-processed` label is applied to the email.
- [ ] **Idempotent re-runs.** Re-execute the Job within the same
  minute. Verify `agent_outputs.crm_updater_runs` shows a new run
  with `messages_processed = 0` (the `secondbrain-processed` label
  excludes already-processed messages).
- [ ] **HIPAA pre-flight rejects HIPAA-domain emails.** Apply
  `secondbrain` to a synthetic test email from a HIPAA-flagged domain
  (toggle one Account.HIPAA on a sandbox row first). Verify the
  Auto-updater audit row records `success=true skipped=true
  reason='HIPAA-flagged participants: ...'` and NO drafts were
  created. Roll back the test HIPAA flag.
- [ ] **Extraction quality smoke.** Process 5 real `secondbrain`
  emails. Eyeball the drafts: are the extracted Tasks actionable?
  Are the Contact updates grounded? Iterate the prompt
  (`prompts/crm_updater/v1.md`) if quality is low; track quality
  improvements in a v1.5 ADR.

## Security checks

- [ ] **DWD scope expansion documented + applied.**
  `docs/dwd_scopes.md` contains `gmail.readonly` + `gmail.modify` rows
  for `asb-agent-triage-sa`. The Workspace Admin Console grant has
  been applied per `docs/runbooks/dwd_scope_expansion_2026-05.md`.
- [ ] **Drafts boundary still holds.** `tests/unit/audit/test_drafts_boundary_check.py`
  tests confirm `gmail.send` STILL fails the audit (the original
  drafts-only invariant from PRD §4.7). Only `gmail.readonly` +
  `gmail.modify` were admitted (label-apply only).
- [ ] **No new DWD-grantable SA.** ADR 0027 §3 invariant preserved —
  `asb-agent-triage-sa` remains the ONLY DWD-grantable SA.
  `asb-crm-updater-sa` impersonates it via resource-scoped
  `iam.serviceAccountTokenCreator` (the binding is on the triage SA
  resource, not at the project level).
- [ ] **Static check forbids `users.messages.send`.**
  `scripts/drafts_static_check.py` continues to forbid
  `users.messages.send` from src/. Run the check locally or
  inspect the latest CI run.
- [ ] **No predefined high-privilege roles.**
  `scripts/least_privilege_check.py` passes — `tbCrmUpdater`
  custom role contains only the four permissions documented in
  `terraform/modules/agent_runtime/crm_updater.tf`.
- [ ] **SA allowlist updated.**
  `asb-crm-updater-sa@agency-brain-demo.iam.gserviceaccount.com`
  + `asb-crm-updater-invoker@...` are in
  `scripts/sa_allowlist_check.py::ALLOWED_EMAILS`.

## Cost checks

- [ ] **Per-run cost ≤ $0.10.** After the first prod run:
  ```sql
  SELECT
    AVG(cost_usd) AS avg_cost,
    MAX(cost_usd) AS max_cost,
    COUNT(*) AS rows
  FROM `agency-brain-demo.agent_audit_log.events`
  WHERE agent_id = 'crm-updater'
    AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
  ```
  Expected: avg_cost ≤ $0.005, max_cost ≤ $0.05.
- [ ] **Monthly burn projection ≤ $5.** At ~10 labeled emails/day,
  monthly burn ≤ $5 — comfortable in the $50/mo envelope (ADR 0024).

## Schema check

- [ ] **`Pending Updates` field exists in airtable_replica.**
  After the airtable-sync image is rebuilt + rolled out (PR B2 step),
  query:
  ```sql
  SELECT pending_updates FROM `agency-brain-demo.airtable_replica.contacts` LIMIT 5;
  SELECT pending_updates FROM `agency-brain-demo.airtable_replica.accounts` LIMIT 5;
  ```
  Expected: query succeeds (column exists). NULL is fine on rows the
  Auto-updater hasn't touched yet.
- [ ] **`agent_outputs.crm_updater_runs` exists.**
  ```sql
  SELECT COUNT(*) FROM `agency-brain-demo.agent_outputs.crm_updater_runs`;
  ```
  Expected: returns a count (≥ 1 after the first run).

## Documentation checks

- [ ] `docs/adr/0047-crm-auto-updater-and-gmail-readonly-scope.md` is
  the canonical decision document. Threat model section addresses
  the DWD scope expansion explicitly.
- [ ] `docs/runbooks/crm_updater.md` (this PR) covers operational
  procedures: how to re-run on demand, how to remove the dedup label
  to re-process a message, how to read `crm_updater_runs`.
- [ ] `docs/runbooks/dwd_scope_expansion_2026-05.md` (PR B2) covers
  the one-time Workspace Admin Console step.
- [ ] `docs/PRODUCTION_STATE.md` deployment table includes the
  `crm_updater` row + scheduler entry.
- [ ] `docs/dwd_scopes.md` reflects the four-scope allowlist.

## v1 limitations (documented, accepted)

- **`Pending Updates` is a long-text field.** Crude review surface
  for v1. v1.5 may promote to a sibling
  `Pending Contact Updates` / `Pending Account Updates` table with
  explicit approval-status singleSelect.
- **Personal-vs-work CRM bucketing is heuristic.** The extractor's
  domain-match rule (account email-domain → work; otherwise →
  personal) may misclassify. Document and revisit after 2 weeks of
  observed extractions.
- **No retry on Gmail API failures.** A transient Gmail API blip
  causes the message to be re-processed on the next tick. Combined
  with the in-app `secondbrain-processed` label dedup, this is
  bounded — at worst we get a duplicate draft, which the human
  approves once.
- **the operator's filter is the relevance gate.** If the `secondbrain`
  Gmail filter stops matching desired emails, the agent stops
  getting input. Operational concern, not architectural.

## Sign-off

- [ ] the implementer — date: ____________
