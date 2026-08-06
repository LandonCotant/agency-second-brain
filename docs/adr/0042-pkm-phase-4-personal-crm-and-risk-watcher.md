# ADR 0042 — PKM merge Phase 4: Personal CRM + Risk Watcher 4th segment

**Status:** Accepted
**Date:** 2026-05-07
**Workstream:** WS-G PKM merge, Phase 4 — ADR 0037 §Phasing

## Context

ADR 0037 (PKM merge architecture) reserved Phase 4 as **Personal
CRM** — the relationship-side surface that complements the project-
side CRM (Operations.Accounts/Contacts/Contracts/Projects). Phase 0a
(notes ingestor + embeddings), Phase 0b (Captures materializer),
Phase 1 (Evening Reflection v2), Phase 2 (Decisions Reviewer), and
Phase 3 (Brag Spotter) are all live in prod (last shipped 2026-05-06,
PR #100). Phase 5 (semantic Connector via `VECTOR_SEARCH`) is
explicitly deferred until the `notes` corpus exceeds ~500 rows.

The user's working hypothesis from ADR 0037 §Phasing line 197–221:
the same multi-segment Risk Watcher loop (ADR 0033 + 0034) that
covers E-commerce, Local Service, and Agency Partner can carry a
fourth `Personal` segment — the rule "no contact in N days" is
shaped exactly like Owner Disengagement, just with a different
recency baseline keyed off relationship type. Adding Personal is a
one-tuple change to the segment loop.

The honest data reality:

- **Operations.Contacts has 2 rows today** (work contacts, both
  linked to client Accounts). No personal contacts captured yet.
- **No `Last Contact` / `Warmth` / `Relationship Type` columns
  exist** — the schema is project-CRM-only.
- The Airtable column add is manual (the sync detects drift and
  publishes to `asb-schema-drift-alerts` but never auto-creates
  columns); ADR 0042 PR-A merge MUST be paired with the user
  adding the four Contacts columns in the Airtable Console.

## Decisions

### 1. Four new Contacts fields

Add to `airtable/schema.json` Contacts block, between `Notes` and
`Account HIPAA` (so the HIPAA cascade lookup stays at the bottom of
the table where the sync filter expects it):

| Field | Type | Options / notes |
|---|---|---|
| `Warmth` | singleSelect | `Hot`, `Warm`, `Cool`, `Cold`. Manually maintained. v1 read-only by the signal (used for severity weighting; see §3); future Personal Brief surfaces this in the morning prompt. |
| `Last Contact` | date | Manually set by the user OR (deferred) auto-updated by a future write surface. The signal reads this directly in v1. |
| `Next Followup` | date | Optional. NOT consumed by the v1 signal — the signal's job is to detect *unintended* recency drift; an explicit followup is the user already on top of it. UI affordance + reserved for future Personal Brief. |
| `Relationship Type` | singleSelect | `Friend`, `Mentor`, `Mentee`, `Collaborator`, `Family`, `Other`. Drives per-relationship-type recency thresholds. Loader gates on this being non-null (see §4). |

Sync layer is zero-code: `airtable/schema.json` → `schema_mapping.py`
→ `airtable_replica.contacts` is fully automatic. The next 15-min
`asb-airtable-sync-15m` tick replicates new columns.

**Rejected: a separate `Personal Contacts` table.** Two CRMs is two
sources of truth; the same Airtable record can be both a work
contact and a personal one (e.g. a former colleague who's now a
mentor). Single table + `Relationship Type IS NOT NULL` gate keeps
work and personal contacts addressable without forcing dual entry.

**Rejected: a separate `Personal` Airtable base.** Same reasoning as
ADR 0020's Operations base collapse — two bases is sync overhead for
a 2-person tool.

### 2. New Risk Watcher segment: `Personal`

Add `Segment.PERSONAL = "Personal"` to `models.py:28-37`. Wire a
4th tuple into `agents/risk_watcher/main.py:119-143` (PR-C):

```python
(
    Segment.PERSONAL,
    build_personal_profile,
    lambda: PersonalClientStateLoader(bq=bq_rows, project_id=project_id),
),
```

Mirrors ADR 0034 §1 exactly. No new Cloud Run Job, no scheduler
change, no new SA, no new TF resources beyond the image tag bump.
Same daily 06:00 PT cadence as the other three segments.

**NOT a Reasoning Engine** — same posture as ADR 0028 + 0029 §3 +
0033 §1. The signal is deterministic threshold math.

### 3. `PersonalReEngagementSignal` — relationship-type-keyed threshold

New file `signals/personal_reengagement.py`. Mirrors
`signals/owner_disengagement.py:34-114` shape.

Inputs from `ClientState.extras`:

- `last_contact_at` (`date | None`): from `contacts.last_contact`.
- `relationship_type` (`str`): drives the threshold.
- `warmth` (`str | None`): drives severity escalation.
- `days_since_contact` (`int | None`): pre-computed by the loader
  via `DATE_DIFF(CURRENT_DATE(), DATE(last_contact), DAY)` — None
  when `last_contact_at IS NULL`.
- `created_at` (`datetime`): contact record creation timestamp,
  used to suppress flags on brand-new contacts that have no
  `Last Contact` set yet.

**Threshold defaults** (encoded in the signal's `__init__`,
overridable per-tick via `client_state.baseline` from
`airtable_replica.risk_profiles`):

| Relationship Type | Default threshold (days) | Rationale |
|---|---|---|
| `Friend` | 60 | Healthy adult-friendship cadence. Not a transaction. |
| `Mentor` | 30 | Mentors expect periodic contact; 30 days respects their time without ghosting. |
| `Mentee` | 30 | Symmetric — initiating regular check-ins is the mentor's lane. |
| `Collaborator` | 21 | Cross-org work contacts; longer drift means the relationship is going stale. |
| `Family` | 90 | Lower-frequency reminder; family contact is too noisy at higher cadence. |
| `Other` | 60 | Same as Friend; conservative default. |

These are pragmatic v1 starting points. Tune in a follow-up ADR
after the first month of real signal output.

**Severity mapping:**

- Days since contact > 2× threshold AND `Warmth IN ('Hot', 'Warm')`
  → `Severity.HIGH` (this is the "you said this person matters and
  it's been a long time" case).
- Days since contact > 2× threshold OR `Warmth = 'Hot'` →
  `Severity.MEDIUM`.
- Otherwise (above threshold but not 2×) → `Severity.LOW`.

`LOW` flags don't dispatch to Chat (the routing matrix's severity
window only triggers Chat on `high`/`critical` per ADR 0023 + 0035).
They DO produce a Gmail draft (drafts always fire — ADR 0032 §3),
which accumulates quietly in the inbox. This matches the user's
"defer-to-batch" preference for personal CRM nudges.

**Suppression on null `last_contact_at`:** if the contact has no
recorded last contact AND was `created_at` > threshold-days ago,
treat the contact's age as the recency proxy. If `created_at` <
threshold, suppress (don't false-fire on contacts the user just
added but hasn't yet logged a contact for). Mirrors the
`OwnerDisengagement` lookback guard at
`signals/owner_disengagement.py:78-82`.

### 4. Loader-side gates

`PersonalClientStateLoader` (new file `loaders/personal.py`) reads
`airtable_replica.contacts` with these filters:

- `relationship_type IS NOT NULL` — excludes work-only contacts
  (which already live in client segments via Account links).
- `hipaa_excluded` aspect propagated through the existing replica
  filter (`Account HIPAA` lookup is null for typical personal
  contacts; the sync's `NOT({Account HIPAA} = TRUE())` formula
  handles null lookups correctly per `sync/hipaa_filters.py`).
- No requirement on `Account` link — personal contacts often have
  no Account. The signal's `account_id` slot stores the Contact
  record id (lowercased, mirroring ADR 0033's namespace
  convention); `project_id` is `NULL`.

The existing `RiskFlagsWriter` same-day dedup
`(account_id, pattern_name, DATE(flagged_at))` reuses cleanly —
each Contact record id is unique, and `pattern_name = 'Personal
Re-engagement'` differentiates from the work-segment flags.

### 5. Routing fan-out: nothing new

`asb-routing-fanout` already polls `risk_flags` (24h lookback,
`WHERE rf.resolved_at IS NULL`, ADR 0033 PR-C + ADR 0035 §5). All
four segments dispatch through the same Chat + Gmail-draft adapters.
The Severity → channel matrix in `routing/matrix.py` already
handles `medium`/`low` flags consistently (Gmail draft always; Chat
only on the 09:00–16:00 PT window for `high`/`critical`).

Zero TF changes for routing. Zero new env vars.

### 6. Three-PR rollout (mirrors ADR 0034)

- **PR-A** (this ADR + schema additions): no behavior change. ADR
  + `airtable/schema.json` Contacts block edits. Manual
  Airtable column add is documented in the PR description.
- **PR-B** (code + tests, no deploy): `signals/personal_reengagement.py`
  + `personal_profile.py` + `loaders/personal.py` + tests +
  `Segment.PERSONAL` enum + `loaders/__init__.py` export. **No
  edit to `main.py`** so PR-B is a strict no-op in prod after
  merge — risk-watcher's segment loop still iterates the original
  three until PR-C.
- **PR-C** (deploy): wire the 4th tuple into `main.py` segment
  loop, rebuild the image as `asb-agents/risk-watcher:adr-0042-personal-segment-v1`, bump the
  TF image tag, targeted apply. Pre-deploy: set `Relationship Type`
  + `Last Contact` on at least 2 Contacts (one inside threshold,
  one outside) so the first prod tick has both signal + non-signal
  cases. Smoke-fire via `gcloud run jobs execute asb-risk-watcher`.

## Consequences

### Positive

- Closes the Personal CRM dimension of the PKM merge with the
  smallest possible blast radius: no new Cloud Run Job, no new
  scheduler, no new SA, no new IAM bindings, no new DWD scope.
- Reuses the multi-segment loop, the routing fan-out, the dedup
  policy, and the audit trail unchanged. The only new surface
  area is one signal class + one loader + one profile.
- Threshold defaults live in code (per-relationship-type), with
  the existing `risk_profiles` Airtable table available as an
  override knob. Tunable without redeploys once the user has
  enough signal data to know what's right.

### Negative / risks

- **Airtable column add is manual.** PR-A merge MUST be paired
  with the user adding the four Contacts columns in the Airtable
  Console; otherwise PR-B's loader returns 0 rows (silent, not
  broken). Documented in the PR-A description.
- **Threshold tuning is data-dependent.** v1 defaults are
  educated guesses. Expect a follow-up ADR after the first
  month if the false-positive rate is too high for any
  relationship type.
- **Suppression on null `last_contact_at` is defensive.** If the
  user adds a personal contact without backfilling
  `Last Contact`, that contact won't fire until `created_at` >
  threshold-days ago. This is the right default (don't nag the
  user about a contact they just added) but means new contacts
  need to be backfilled to be useful immediately.
- **No "snooze" surface.** Resolved flags (`risk_flags.resolved_at
  IS NOT NULL`) are excluded from re-dispatch (ADR 0035 §5), but
  the user has to manually UPDATE the BQ row to mark a flag
  resolved — same operational pattern as Decisions Reviewer
  (ADR 0041). Acceptable for v1; a real "snooze for 30 days"
  affordance is a follow-up.
- **Personal contacts may have no Account.** The `account_id`
  slot in `risk_flags` stores the Contact record id for the
  Personal segment, which breaks the convention that
  `risk_flags.account_id` JOINs to `airtable_replica.accounts`.
  Consumers querying by segment first will be fine; ad-hoc
  queries that JOIN unconditionally will return null for
  Personal rows. Document this in the closeout.

### Rejected alternatives

- **Per-contact thresholds in Airtable** (a `Followup Cadence`
  override field on Contacts). Adds a column the user has to
  maintain manually for every contact. Defaults-by-relationship-
  type carry their weight without that overhead.
- **Always-CRITICAL severity for Personal flags.** Tested
  conceptually against the routing matrix — would put every
  stale-friend nudge into the same Chat batch as a Client A
  client crisis. Severity needs to differentiate. The
  Warmth-weighted `HIGH/MEDIUM/LOW` mapping carries the urgency
  signal without poisoning the work-priority channel.
- **A separate "Personal Brief" Cloud Run Job** that summarizes
  pending Personal flags into a weekly digest. Right shape *if*
  the daily Risk Watcher tick produces enough Personal flags to
  drown out work flags. It probably won't (the threshold math is
  conservative). Reopen if first-month volume justifies it.

## References

- ADR 0033 — Risk Watcher topology + signal-as-data
- ADR 0034 — Risk Watcher follow-up profiles (multi-segment loop pattern)
- ADR 0035 — Owner Disengagement v2 (resolved-flag filter)
- ADR 0037 — PKM merge architecture (Phase 4 reservation)
- PRD §6.4 — Signal Protocol contract
- `src/agency_brain/agents/risk_watcher/signals/owner_disengagement.py` — signal analog
- `src/agency_brain/agents/risk_watcher/loaders/ecommerce.py` — loader analog
- `src/agency_brain/agents/risk_watcher/main.py:119-143` — multi-segment loop
