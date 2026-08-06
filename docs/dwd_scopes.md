# Domain-Wide Delegation Scopes

Per [PRD.md](../PRD.md) §4.3, every Workspace DWD scope grant is documented here with the requesting service account, justification, and grant date. PRs that change scope require explicit reviewer approval.

The DWD subject is `owner@example.com` — the human reviewer's mailbox. Drafts created by agents land directly in his Drafts folder for review-then-send. The WS-A scaffolding originally documented `brain-agent@example.com` as a default subject; ADR 0027 supersedes that for WS-G* agents because the review-and-send UX completes in the human reviewer's familiar Gmail surface.

This file is the source of truth for the runtime drafts-boundary audit (see [ADR 0027](adr/0027-dwd-delegation-surface.md), [ADR 0029](adr/0029-morning-brief.md), [ADR 0047](adr/0047-crm-auto-updater-and-gmail-readonly-scope.md), and `src/agency_brain/audit/drafts_boundary_check.py`). The audit parses the table below and asserts every scope is on the allowlist (`{gmail.compose, calendar.readonly, gmail.readonly, gmail.modify}` per ADR 0047). Adding a new scope or SA requires a superseding ADR.

## Granted scopes

| Service Account | Scope | Justification | Granted Date | Workstream |
|---|---|---|---|---|
| `asb-agent-triage-sa` | `gmail.compose` | WS-G1 Triage Agent drafts replies into the operator's mailbox for review-then-send. Drafts-only per PRD §4.7. | 2026-05-01 | WS-G1 |
| `asb-agent-triage-sa` | `calendar.readonly` | WS-G3 Morning Brief reads today's calendar (the operator's primary) for prep notes in the daily brief. Read-only — cannot create/modify/delete events. | 2026-05-02 | WS-G3 |
| `asb-agent-triage-sa` | `gmail.readonly` | CRM Auto-updater reads bodies of `secondbrain`-labeled threads to extract task / contact / account drafts. Read-only on inbox; write goes to Airtable as drafts (PRD §4.7). Threat model + mitigations in [ADR 0047](adr/0047-crm-auto-updater-and-gmail-readonly-scope.md). | 2026-05-09 | CRM Auto-updater |
| `asb-agent-triage-sa` | `gmail.modify` | CRM Auto-updater applies `secondbrain-processed` label to messages after successful draft creation (idempotent dedup). `users.messages.send` blocked at PR-gate static check. | 2026-05-09 | CRM Auto-updater |
