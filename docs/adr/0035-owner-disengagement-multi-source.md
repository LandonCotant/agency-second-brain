# ADR 0035 — Owner Disengagement: multi-source engagement detector

**Status:** Accepted
**Date:** 2026-05-05
**Supersedes:** ADR 0034 §5 (OwnerDisengagement spec only — the rest of ADR 0034 stands)

## Context

ADR 0034 §5 shipped Owner Disengagement (Local Service) as a calendar-only
heuristic: fire when the agency owner has had no Calendar events with
attendees from any of the client's contact-email **domains** in the
threshold window. The intent was "client domain disengagement"; the
implementation was attendee-domain-suffix match.

First production tick (2026-05-04, image `adr-0034-risk-watcher-v3`,
execution `asb-risk-watcher-k8kd8`) fired two CRITICAL false positives:

| flag_id | account | signal_evidence |
|---|---|---|
| `844dacbc-...` | Client A (`recTqak9Fn8d8pmkf`) | "Owner had no meetings with attendees at gmail.com in the last 30 days" |
| `30dc3bd7-...` | Client C Studio (`recFSjATvyj6Sza5C`) | "Owner had no meetings with attendees at icloud.com in the last 30 days" |

Both contacts use free-mail providers (gmail.com, icloud.com), so the
domain-suffix match reduces to "did the owner meet with ANYONE on
gmail.com / icloud.com" — a near-tautology that has nothing to do with
client engagement. Both flags fully dispatched (Chat 200 + Gmail draft)
within ~5 min via the routing fanout.

Two structural problems with v1:

1. **Calendar-only misses async engagement.** Email replies, task
   reviews, deliverable approvals, even meetings the contact didn't
   attend in person — all genuine engagement, none captured.
2. **Domain-suffix matching is wrong for free-mail providers.** Anyone
   meeting at gmail.com is "the client" under v1 logic. The fix isn't
   to skip free-mail (a `FREE_MAIL_DOMAINS` filter would just produce
   silent zero-coverage); it's to stop matching on domain at all.

## Decisions

### 1. Multi-source engagement aggregation

Per LS account passing the gate (§2 below), compute:

```
last_engagement_at = MAX(
    last_calendar_event_with_crm_attendee,   # exact-email match
    last_triage_inbound_at,                  # MAX(tasks.created) WHERE source='Triage Agent'
    last_approved_or_done_task_at,           # MAX(tasks.last_modified) WHERE approval_status='Approved' OR status='Done'
)
```

Signal fires when `now - last_engagement_at >= threshold`. If all three
sources are quiet within the lookback window AND `lookback >= threshold`,
the signal also fires (with evidence "no engagement in last N days").
If `lookback < threshold` the signal is quiet — same posture as v1's
short-lookback guard, prevents false-fire on freshly-loaded clients.

The MAX is taken across heterogeneous sources because each captures a
different facet of engagement:

- **Calendar** — synchronous engagement (in-person/Zoom meetings).
  Matches via exact attendee email against the CRM contacts list, not
  domain. Free-mail noise disappears.
- **Triage inbound** — asynchronous inbound (emails the client sent
  that landed in our triage pipeline). Reuses the E-comm loader's
  `tasks.source = 'Triage Agent'` proxy with `tasks.created` as the
  inbound timestamp; same join chain, no extra dataset hop into
  `triaged_items`. NOTE: `dedup_skipped` triages and "no project match"
  triages don't produce a Tasks row, so they don't count — acceptable
  because both are by-design cases without a resolved account.
- **Approved-or-done tasks** — owner-side activity (the operator approving
  a Triage draft, marking a deliverable Done, editing an existing
  Approved task). Strict filter (`approval_status = 'Approved' OR
  status = 'Done'`) so a fresh Triage Agent draft doesn't count as
  engagement until the operator actually acts on it.

`winning_source` (the source whose timestamp won the MAX) is surfaced
into `signal_evidence` so the Chat card / Gmail draft tells the
operator which engagement type to look at.

### 2. Loader-side gating: active contract + has CRM contact

`LocalServiceClientStateLoader._load_accounts` SQL adds two `EXISTS`
clauses:

```sql
AND EXISTS (
  SELECT 1 FROM `<project>.airtable_replica.contracts` c
  WHERE c.account[OFFSET(0)] = a._airtable_record_id
    AND c.contract_status = 'Active'
    AND (c.expiry_date IS NULL OR c.expiry_date >= CURRENT_DATE())
)
AND EXISTS (
  SELECT 1 FROM `<project>.airtable_replica.contacts` ct
  WHERE ct.account[OFFSET(0)] = a._airtable_record_id
    AND ct.email IS NOT NULL
)
```

Accounts failing either gate are dropped from the loader entirely —
Owner Disengagement *can't* exist for them. Contract gate ensures we
only flag clients we're actively serving (no churned-but-still-Active
accounts). Contact-email gate ensures we have someone to match
calendar attendees against.

### 3. Calendar matching: exact email only

`CalendarClient` API replaces `last_meeting_with_domain` with:

```python
def most_recent_engagement_event(
    self,
    *,
    owner_email: str,
    attendee_emails: tuple[str, ...],
    since: datetime,
    until: datetime,
) -> datetime | None: ...
```

Returns max event start where any attendee's lowercased email is in
the lowercased `attendee_emails` set. No domain-suffix match. No
event.summary keyword match. Returns None on Calendar API failure
(loader handles by setting `last_engagement_at` from the other two
sources only).

`event.summary` keyword matching against `account.company_name` was
considered and rejected: the matching is fuzzy ("Client A" vs "ClientA
Private Investigations" vs "ClientA"), and the active-contract gate
already filters to accounts where engagement is expected. The other
two sources (triage + approved tasks) cover the case where the operator
worked with the client outside calendar.

### 4. Strict task-engagement filter

Only `approval_status = 'Approved' OR status = 'Done'` tasks count
toward `last_approved_or_done_task_at`. A pending Triage-drafted task
does NOT count as the operator engagement — it's bot-side activity. Once
the operator acts (approves, edits, marks done), that bumps `last_modified`
AND moves the task into the matching status, so it counts from that
moment forward.

### 5. Routing filter: `resolved_at IS NULL`

`build_risk_flags_poll_query` adds `AND rf.resolved_at IS NULL` to
the WHERE clause. Resolved flags don't dispatch. Standalone defensive
value beyond this PR — manual or future agent-side resolution can
suppress flag fan-out without DDL changes.

### 6. v1 false-positive cleanup

Manual one-shot UPDATE post-merge:

```sql
UPDATE `agency-brain-demo.agent_outputs.risk_flags`
SET resolved_at = CURRENT_TIMESTAMP(),
    resolution_note = 'v1 calendar-only false positive; superseded by ADR 0035 multi-source design'
WHERE flag_id IN (
  '844dacbc-355c-45db-86e4-6c2f58bce6ca',
  '30dc3bd7-f204-45b3-ad0a-612b9816f959'
)
  AND resolved_at IS NULL;
```

Both flags are already dispatched (`routed_events` confirms Chat 200 +
Gmail draft for each at 18:06 UTC). Marking resolved is audit-only;
dedup already prevents re-dispatch. Together with §5 it keeps
`resolved_at IS NULL` as a meaningful filter going forward.

## Alternatives considered

- **Add `FREE_MAIL_DOMAINS` skip-list to v1.** Would zero-out the false
  positives but also zero-out coverage for the actual majority of LS
  clients (most service-business contacts use free-mail). The
  exact-email match (§3) achieves the same noise filter without the
  silent zero-coverage failure mode.
- **Switch to `accounts.last_activity_timestamp` as the single
  source.** Captures any Airtable-side change including the sync's own
  rollup recomputes. Poisoned signal — a sync-only update would look
  like engagement. Rejected.
- **`event.summary` keyword match against company name.** Substring
  match is fuzzy and the active-contract gate handles the same
  "engagement-expected" filtering more cleanly. Reopen if a real
  false-negative shows up.
- **Add `gmail.readonly` DWD scope.** Would let us read inbound email
  content directly. Bigger architectural change — new ADR superseding
  ADR 0027 §2 (one DWD-grantable SA), drafts-only boundary loosens.
  The triage-inbound proxy already gives us 95% of what we need from
  inbound emails (we already classify all of them).

## Consequences

### Positive

- Free-mail accounts (Client A, Client C Studio, most LS clients)
  are correctly evaluable. v2 won't fire on them unless engagement is
  genuinely stale across all three sources.
- Active-contract gate prevents legacy / churned accounts from
  flagging.
- Multi-source aggregation captures the realistic shape of agency
  engagement (sync + async + owner-side action).
- ADR 0034 §1–§4 stand unchanged — this is a localized signal
  rebuild, not a systemic change. AP skeleton (ADR 0034 §5) and the
  multi-segment loop are unaffected.

### Negative / risks

- **First-run baseline is empty.** A brand-new account has no triage
  history, no approved tasks, and no calendar events with the contact
  yet. The lookback-vs-threshold guard prevents false-fire here, but
  the signal is also genuinely silent for new clients until they
  generate engagement. Acceptable — we're not trying to detect
  problems with brand-new accounts.
- **Contract-gate dependency on Airtable hygiene.** If the Operations
  base's `Contracts.Status` isn't kept current, a churned account
  with a still-"Active" contract will still flag. Mitigation: the
  next signal refinement could read `contracts.expiry_date` more
  strictly.
- **Calendar API outage = degraded signal.** If Calendar is down, we
  fall back to triage + tasks only. Acceptable degradation; logged
  via the loader's existing exception path.

## References

- ADR 0019 — `Team.User._extract: user_id` annotation (only field that
  opts out of the email default)
- ADR 0022 — Triage Agent same-client domain-match guard + free-mail
  skip-list (where the rejected approach lives)
- ADR 0026 — `dedup_skipped` triage rows (don't produce Tasks rows;
  consequence for the triage-inbound proxy)
- ADR 0027 — DWD scope allowlist; `gmail.readonly` not on it
- ADR 0033 — Risk Watcher topology (one Job, daily tick)
- ADR 0034 — Risk Watcher follow-up profiles (LS, AP skeleton, G2d
  deferred); §5 OwnerDisengagement spec is what this ADR supersedes

## Manual operational note

After PR merge + image rebuild as `adr-0035-risk-watcher-v1`:

1. `gcloud run jobs update asb-risk-watcher --image=...:adr-0035-risk-watcher-v1`
2. **Pause for explicit approval**, then run the §6 UPDATE.
3. Force-fire the Job and confirm the audit row + the absence of new
   false-positive flags for Client A and Client C Studio.
4. The 06:00 PT scheduler tomorrow validates the daily cadence path.
