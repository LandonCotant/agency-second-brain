# ADR 0027 — Workspace Domain-Wide Delegation surface for the Triage Agent

**Status:** Accepted
**Date:** 2026-05-01
**Workstream:** WS-G1 (Triage Agent) + WS-F (security)

## Context

WS-D Chat fan-out shipped (ADR 0023, 0025), and the triage pipeline is now
deduped (ADR 0026). The next outbound channel — Gmail draft fan-out, plus
the WS-G3 Morning Brief — both depend on Workspace Domain-Wide Delegation
(DWD). Until 2026-05-01 DWD was the strategic blocker for the entire
right-hand side of the routing matrix: PRD §4.7 mandates drafts-only Gmail
access, which on Workspace requires impersonation via DWD.

DWD was provisioned manually on 2026-05-01:

- **Service account:** `asb-agent-triage-sa`
  (`asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com`)
- **OAuth scope:** `https://www.googleapis.com/auth/gmail.compose`
- **Subject:** `owner@example.com`
- **Verified:** one-shot `users.drafts.create` succeeded against the operator's
  mailbox; the draft landed in Drafts and was visible in the Gmail UI.

Two follow-on items are now stale:

1. The runtime drafts-boundary audit
   (`src/agency_brain/audit/drafts_boundary_check.py`) still carries a
   docstring at lines 19–26 noting the OAuth scope check is "out of scope
   for PR #2 ... Re-introduce when WS-G1 ships." WS-G1 has shipped.
2. Two pieces of Terraform drift were created during the manual
   provisioning: `gmail.googleapis.com` was enabled out-of-band for the
   smoke, and `roles/iam.serviceAccountTokenCreator` was granted manually
   to `user:owner@example.com` on `asb-agent-triage-sa` to
   support ADC impersonation in deploy/admin work. Both need codifying.

This ADR documents the delegation surface and the audit posture chosen to
defend it.

## Decision

### 1. Subject deviates from the WS-A scaffolding default

`docs/dwd_scopes.md` (created by WS-A) states the default DWD subject is
`brain-agent@example.com` — a non-human Workspace user. We
deviate: the Triage Agent impersonates `owner@example.com`
(the human reviewer) so drafts land directly in the operator's own Drafts
folder. He reviews and sends from his familiar Gmail surface.

**Why:** the alternative is creating drafts in a shared
`brain-agent@` mailbox that the operator then has to context-switch to, defeating
the "drafts-only review-then-send" UX. The human reviewer's mailbox is
where the work actually completes. Risk envelope is acceptable because
`gmail.compose` cannot send (only `users.messages.send` /
`users.messages.modify` can — neither is granted) and the operator sees every
draft before any client communication leaves the org.

`docs/dwd_scopes.md` is updated to reflect this and to populate the
previously empty granted-scopes table.

### 2. `gmail.compose` is the only DWD scope, ever (until a new ADR)

The drafts-only boundary (PRD §4.7) is the load-bearing constraint with
Model Armor disabled at runtime (ADR 0017). `gmail.compose` permits
draft creation but not sending; we never construct
`users.messages.send` or `users.messages.modify` calls. Future agents
(Morning Brief, routing Gmail fan-out) will reuse the **same** scope on
the **same** SA. Adding any other Gmail scope (`gmail.send`, `gmail.modify`,
`gmail.metadata`, etc.) requires a superseding ADR.

### 3. Audit posture — doc-driven allowlist (not live Admin SDK)

The drafts-boundary audit re-introduces an OAuth scope check, but uses
**`docs/dwd_scopes.md` as the source of truth**, not the Workspace Admin
SDK. The audit:

- Parses the granted-scopes table in `docs/dwd_scopes.md`.
- Asserts every row's scope is in `_ALLOWED_DWD_SCOPES = {"gmail.compose"}`.
- Any row outside the allowlist → drift row in `agent_audit_log.events`
  (event_id `SECURITY_DRIFT`), routed to the existing Chat alert via the
  same path as the IAM check.
- Empty table is tolerated (the pre-DWD state).

#### Why not the Admin SDK Directory API

A live check would call `admin.directory.domain.readonly` against
Workspace, but the audit SA itself has no DWD grant — and granting one
just to read DWD config is chicken-and-egg, costs another reviewer
ceremony per WS-A's `dwd_scopes.md`, and adds an Admin SDK surface for
marginal benefit on a 2-person tool. Per user-memory
`feedback_security_vs_cost.md`: pragmatic security over PRD-prescribed
defense-in-depth when marginal cost is real and marginal risk is low.

The doc-driven design pairs **two** independent checks that already
exist elsewhere:

- **Reviewer discipline** on `docs/dwd_scopes.md` PRs (already required
  by PRD §4.3 — "PRs that change scope require explicit reviewer
  approval").
- **A static repo guard** at `scripts/drafts_static_check.py`
  (PR-gate) that fails the build if any Python file under `src/`
  references `gmail.send` / `users.messages.send` /
  `users.messages.modify`. This catches the case where someone adds a
  send code path without updating the doc, or vice versa.

A live Workspace check can be added in the future as ADR 002X if we ever
have multiple DWD subjects or scopes drifting from the doc. Today, with
one SA + one scope + one reviewer, the doc IS the surface.

### 4. Codify the manual TF drift

Two follow-ups, both in this PR:

- `terraform/modules/foundation/main.tf` — add `gmail.googleapis.com`
  to `local.brain_apis`. Currently enabled out-of-band 2026-05-01 for
  the DWD smoke; codifying it makes the drift go away on the next
  `terraform plan`.
- `terraform/modules/agent_runtime/triage_agent_iam.tf` — add a
  `google_service_account_iam_member` granting
  `roles/iam.serviceAccountTokenCreator` on `asb-agent-triage-sa` to
  `user:owner@example.com`. This is the binding that lets
  the operator impersonate the SA via ADC for deploy/admin work
  (`deploy_triage_re.py` etc.). The grant is **on the SA itself**, not
  project-wide — `gcloud iam service-accounts add-iam-policy-binding`
  semantics, narrow blast radius.

## Consequences

**Positive**

- The drafts-only boundary is now enforced by both a runtime check
  (daily via the existing `asb-audit-drafts-boundary` Cloud Run Job) and
  a PR-gate static check. WS-G3 + Gmail fan-out land on a defensible
  baseline.
- `terraform plan` becomes clean again; no manual gcloud steps live
  outside the repo.
- Future agents (Morning Brief, Gmail fan-out) cost zero new DWD
  ceremony — they reuse `asb-agent-triage-sa`'s existing grant. If we
  later split SAs per agent, each one gets its own row in
  `dwd_scopes.md` and the audit allowlist still holds.

**Negative / accepted**

- The doc-driven audit cannot detect Workspace-side drift if a Workspace
  admin grants additional scopes outside the repo. Mitigated by:
  (a) Workspace admin == the operator, (b) the DWD UI requires explicit
  scope strings — there is no "everything Gmail" shortcut, (c) the
  `asb-agent-triage-sa` description in TF says
  `No Gmail send/modify per PRD §4.7 drafts boundary` — anyone touching
  it has the constraint in front of them.
- The static repo guard is a string match. It will false-positive on
  any docstring or comment that uses `gmail.send` as illustrative
  text. The script tolerates this by allowlisting test fixtures and
  by flagging only `import` / call-site syntax.

## Rollout

1. Land the PR with code + tests + this ADR + the two TF drift fixes.
2. Targeted apply:
   `terraform apply -target=module.foundation.google_project_service.brain -target=module.agent_runtime.google_service_account_iam_member.triage_token_creator_landon`.
   Both are no-op against live state (the API is already enabled, the
   binding already exists) — confirms drift is resolved.
3. Manually trigger the runtime audit:
   `gcloud run jobs execute asb-audit-drafts-boundary --region=us-central1 --project=agency-brain-demo`.
   Verify the audit row in `agent_audit_log.events` shows
   `event_id = "SECURITY_AUDIT_OK"` with `dwd_scopes_checked: 1` in the
   summary.
4. Smoke drift detection on a throwaway branch: add a fake
   `gmail.send` row to `docs/dwd_scopes.md`, run
   `python -m agency_brain.audit.drafts_boundary_check` locally,
   confirm `event_id = "SECURITY_DRIFT"`. Discard the branch.

## References

- PRD §4.3 (DWD scope governance), §4.7 (drafts-only boundary)
- ADR 0017 (Model Armor disabled at runtime — drafts-only carries more
  weight as a result)
- ADR 0023 (Chat fan-out — first channel; documented why it doesn't
  need a DWD grant)
- ADR 0026 (Triage dedup — the immediately preceding closeout)
- `src/agency_brain/audit/drafts_boundary_check.py` (the audit module)
- `docs/dwd_scopes.md` (the source-of-truth doc)
- `scripts/drafts_static_check.py` (the PR-gate static check)
