# ADR 0029 — Morning Brief topology + DWD scope expansion to `calendar.readonly`

**Status:** Accepted
**Date:** 2026-05-02
**Workstream:** WS-G3 (Morning Brief)

## Context

PRD §5.6 names "the operator using Brain daily (morning brief)" as the week-5
milestone, and PRD §V1 launch criterion #2 (line 484) puts "≥14 days of
daily briefs" on the v1 ship checklist. The proactive-agency spec at
`docs/source/proactive_agency_brain_spec.md:344-356, 1003-1024` defines
the brief as a five-section markdown email — Today's Top 3, Drafts
Awaiting Review, Risk Flags, Calendar, Rest of Queue — drafted into the
recipient's mailbox 5 minutes before their morning processing time.

ADR 0027 established the DWD delegation surface: one SA
(`asb-agent-triage-sa`), one scope (`gmail.compose`), one subject
(`owner@example.com`), drafts-only per PRD §4.7, doc-driven
audit posture. ADR 0027 §2 explicitly says "Adding any other Gmail
scope ... requires a superseding ADR." This ADR is that superseding ADR
— it expands the scope set to include Calendar read-only, and pins the
Morning Brief topology that consumes both scopes.

This ADR is split across two PRs (governance/PR-A, agent code/PR-B) so
the IAM + scope surface lands first and PR-B's review can focus on agent
logic. See "Rollout" below.

## Decision

### 1. Reuse `asb-agent-triage-sa` (no new SA)

Per ADR 0027 §2: one SA / one delegation surface. The Morning Brief
agent runs as `asb-agent-triage-sa`, sharing the existing DWD grant.
Adding a second SA would double the Workspace ceremony, the audit
surface, and the dwd_scopes.md row count, for no operational benefit
on a 2-person tool. Both Triage and Morning Brief will write
`agent_audit_log.events` rows; they're distinguished by `agent_id`
(`triage-bridge` vs `morning-brief`), not by SA email.

### 2. Expand DWD allowlist to `{gmail.compose, calendar.readonly}`

Add `https://www.googleapis.com/auth/calendar.readonly` to the existing
DWD grant on `asb-agent-triage-sa` (Workspace Admin Console manual step,
done before PR-A merges). Update:

- `docs/dwd_scopes.md` — append the calendar row.
- `src/agency_brain/audit/drafts_boundary_check.py:60` — bump
  `_ALLOWED_DWD_SCOPES` to `frozenset({"gmail.compose", "calendar.readonly"})`.

**Drafts-only still holds.** Calendar access is read-only (cannot create
events, cannot modify, cannot delete); Gmail access is compose-only
(cannot send). Neither scope crosses the PRD §4.7 boundary. The
`scripts/drafts_static_check.py` PR-gate continues to block
`.messages().send`, `.messages().modify`, and `gmail.send` /
`gmail.modify` literals.

### 3. Topology — Cloud Run Job + daily scheduler + Vertex SDK direct

PR-B will land the agent module at `src/agency_brain/agents/morning_brief/`
with this shape:

- **BaseAgent subclass** (`MorningBriefAgent`). `_run` reads source
  data → renders prompt → calls Vertex `gemini-2.5-flash` direct via
  the SDK → parses → dedup pre-check → drafts Gmail → writes BQ row.
  BaseAgent handles audit emission per PRD §4.6 / ADR 0006.
- **Vertex SDK direct, NOT a Reasoning Engine.** Avoids ADR 0028's
  CreateReasoningEngine alert and the orphan-RE cost incident posture
  (2026-05-02). One Cloud Run Job execution = one Vertex API call.
- **Daily scheduler** at `25 7 * * *` with
  `time_zone="America/Los_Angeles"`. Cloud Scheduler handles DST.
- **One Cloud Run Job** (`asb-morning-brief`) running as
  `asb-agent-triage-sa`, image
  `us-central1-docker.pkg.dev/.../asb-agents/morning-brief:adr-0029-morning-brief-v1`.

### 4. Per-recipient-per-local-date dedup

Mirroring ADR 0026's input_hash idiom, the writer pre-checks
`agent_outputs.morning_briefs` for an existing row matching
`(recipient_email, local_date)`. Hit → skip Gmail draft + BQ insert,
mark `dedup_skipped=true` on the audit row. Miss → proceed.

Unlike ADR 0025, this is a SELECT (not DML) on a row volume of
~1/day/recipient — streaming-buffer constraints don't apply.

### 5. "Always draft, even on quiet days"

If every section is empty (no triaged items, no open tasks, no risk
flags, no awaiting drafts, no calendar events), still draft a brief
with a one-line "Quiet day" body. The daily ritual is the load-bearing
user behavior; skipping breaks the habit. Empty-day bodies are short
enough that the cost-per-draft delta is negligible.

### 6. Single recipient for v1

`BRIEF_RECIPIENTS` env var = `owner@example.com`.
Multi-recipient (leadership view, week-6 spec) is deferred to a
superseding ADR once the brief shape is settled.

## Consequences

**Positive**

- Load-bearing PRD week-5 milestone shipped; 14-day daily-brief streak
  becomes possible on Day 1.
- DWD surface now defensibly used by two agents (Triage, Morning Brief)
  under one SA with one explicit allowlist. Future Gmail-using agents
  reuse without ceremony unless they need a NEW scope.
- The audit + drafts-static-check pair (ADR 0027) automatically covers
  the new scope without code change beyond the allowlist bump.

**Negative / accepted**

- Prompt cost: ~$0.001/day at gemini-2.5-flash rates for a single
  recipient. Negligible at the 2-person scale.
- One extra BQ partition (`agent_outputs.morning_briefs`, 730d TTL per
  ADR 0024).
- One extra image on AR (`asb-agents/morning-brief`), under the
  recent-5 + 90d cleanup policy from ADR 0024.

## Rollout

### PR-A (governance + scope expansion, this ADR)

Pre-requisite: Workspace Admin → Security → API Controls → Domain-wide
delegation. Edit the existing grant for client ID
`asb-agent-triage-sa@…` to add
`https://www.googleapis.com/auth/calendar.readonly`. Smoke with a
one-shot `events.list(calendarId='primary')` impersonation call. ADC
must be live on the operator's machine.

Files in PR-A: this ADR, `docs/dwd_scopes.md` (append row),
`src/agency_brain/audit/drafts_boundary_check.py` (allowlist bump),
`terraform/modules/foundation/main.tf` (`calendar.googleapis.com` to
`local.brain_apis`), `tests/unit/audit/test_drafts_boundary_check.py`
(extend).

Post-merge:
1. `terraform apply -target=module.foundation.google_project_service.brain["calendar.googleapis.com"]`.
2. Rebuild + push asb-audit image with new tag (e.g.
   `:adr-0029-calendar-scope`) via `cloudbuild.audit.yaml` (PR #58).
3. Update the four `asb-audit-*` Cloud Run Jobs to point at the new
   image.
4. Manually run `asb-audit-drafts-boundary` and confirm the latest
   `agent_audit_log.events` row shows `dwd_scopes_checked: 2` and
   `event_id: SECURITY_AUDIT_OK`.

### PR-B (agent code, separate PR)

Lands after PR-A merges + applies. Agent module + new BQ table + Cloud
Run Job + scheduler + image build + tests + Codex/Claude doc refresh.
See plan file at `~/.claude/plans/once-i-merge-pr-goofy-anchor.md` for
the full file list and verification sequence.

## References

- PRD §5.6 (week-5 milestone), §V1 launch criterion #2, §4.7
  (drafts-only)
- ADR 0006 (BaseAgent audit contract)
- ADR 0017 (Model Armor disabled at runtime — drafts-only carries
  weight as a result)
- ADR 0024 (cost guardrails — 730d BQ TTL inherits automatically)
- ADR 0026 (dedup-via-SELECT pattern reused here)
- ADR 0027 (DWD delegation surface — this ADR supersedes only its
  allowlist constant; the doc-driven audit posture, subject choice,
  and reviewer discipline remain)
- ADR 0028 (CreateReasoningEngine alert — Morning Brief uses Vertex
  SDK direct, not RE, so the alert won't fire on Morning Brief
  activity)
- `docs/source/proactive_agency_brain_spec.md:344-356, 1003-1024`
  (brief composition spec)
- `docs/source/agency_goal_hierarchy_v0.1.md:141` (quarterly goal)
