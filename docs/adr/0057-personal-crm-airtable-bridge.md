# ADR 0057 — Personal CRM: Airtable → Brain Galaxy bridge

**Status:** Accepted — 2026-05-18. Extends ADR 0042 (Personal CRM + Risk Watcher 4th segment), ADR 0054 §2 (Galaxy drop-to-index), ADR 0053 (wikilinks + related_notes), ADR 0044 (Drive write via folder-share + ADC). Does not supersede any.

## Context

The Brain has an Airtable-canonical CRM layer (ADR 0042) — `Contacts` with `Warmth`, `Relationship Type`, `Last Contact`, `Next Followup` plus `Accounts` for client orgs. Risk Watcher's `PersonalReEngagementSignal` runs against this data.

What's missing is the **prose layer**: the freeform "How I know them" / "Conversation log" / "Connections" content from the Obsidian personal-CRM pattern (user's reference document, 2026-05-18) that doesn't fit Airtable columns but is the soul of a personal CRM. Today, wikilinks like `[[Client A]]` in Morning Briefs don't resolve to anything because no Brain-side note exists with `filename` matching `client a` (per ADR 0053's resolution rules).

Goal: bridge Airtable rows → Drive markdown files → `agent_outputs.notes` rows so:

1. Wikilinks resolve into `notes_links` graph edges (per ADR 0053)
2. `brain_ask "tell me about Client A"` surfaces the right context via VECTOR_SEARCH
3. The user has a Drive surface to add freeform prose that grows over time
4. New Airtable rows automatically land in Brain (no manual stub creation)

## Decision

A new Cloud Run Job `asb-people-sync` bridges `airtable_replica.{accounts,contacts}` → Drive markdown files under `Brain/05_GALAXY/{01_ACCOUNTS,02_CONTACTS}/`. The existing Librarian Galaxy sweep (ADR 0054 §2) then picks them up and writes `agent_outputs.notes` rows with `note_kind='galaxy'`, completing the loop.

### §1 — Storage layout

| Source table | Drive folder | Filename | Note kind (post-Librarian) |
|---|---|---|---|
| `airtable_replica.accounts` | `Brain/05_GALAXY/01_ACCOUNTS/` | `<Accounts.Name>.md` (verbatim from Airtable) | `galaxy` |
| `airtable_replica.contacts` | `Brain/05_GALAXY/02_CONTACTS/` | `<Contacts.Name>.md` (verbatim from Airtable) | `galaxy` |

**Filename source**: Airtable display Name verbatim, with only Drive-illegal characters stripped (`/`, `:`, `?`, `*`, `"`, `<`, `>`, `|`). Whitespace preserved. The wikilink resolver matches case-insensitively on filename (ADR 0053), so `[[Client A]]` resolves to `Client A.md`.

**Slug collisions**: when two Contacts share a Name verbatim, the second one's filename appends `(Organization)` disambiguator. The `airtable_id` field in frontmatter is the actual primary key.

### §2 — Accounts frontmatter schema

```yaml
---
type: account
airtable_id: recXXXXXXXXXXXXXX
name: "Client A"
status: active                              # active | inactive | prospect | churned | archived
hipaa: false                                # mirrors Accounts.HIPAA — if true, file is NOT SYNCED
industry: "Investigations"                  # from Accounts.Industry
account_owner: "owner@example.com"  # from Accounts.Account Manager (email-extracted)
relationship_type: client                   # always 'client' for Accounts rows
drive_folder_url: ""                        # from Accounts.Google Drive Folder (may be empty)
synced_at: 2026-05-18T15:00:00Z             # last successful sync of frontmatter
---
```

Body skeleton (only written on first creation; **never overwritten** thereafter):

```markdown
# Client A

## Who they are
<empty — user adds prose>

## Active engagements
<!-- AUTO: Active Projects from airtable_replica.projects WHERE account_id = this -->

## Recent activity
<!-- AUTO: last 10 triaged_items + calendar events involving this account -->

## Open risks
<!-- AUTO: open risk_flags WHERE account_name = this -->

## Connections
<empty — user adds wikilinks: [[Contact Name]], [[Project Name]], etc.>
```

### §3 — Contacts frontmatter schema

```yaml
---
type: person
airtable_id: recXXXXXXXXXXXXXX
name: "First Last"
email: "first@example.com"
role: "MBA Candidate"                       # from Contacts.Role
organization: "Ross School of Business"     # from Contacts.Organization
relationship_type: classmate                # from Contacts.Relationship Type (single-select)
warmth: warm                                # from Contacts.Warmth — hot | warm | cool | cold | new
last_contact: 2026-03-28                    # from Contacts.Last Contact
next_followup: 2026-04-15                   # from Contacts.Next Followup
linkedin: ""                                # from Contacts.LinkedIn
phone: ""                                   # from Contacts.Phone
tags: []                                    # from Contacts.Tags (multi-select)
synced_at: 2026-05-18T15:00:00Z
---
```

Body skeleton (only on first creation):

```markdown
# First Last

## Who they are
<empty — user adds prose>

## How I know them
<empty — user adds prose>

## Conversation log
<!-- AUTO: each entry is "### YYYY-MM-DD — <source>" with a bullet list -->

## Connections to my work
<empty — user adds wikilinks>
```

### §4 — Sync semantics

- **Cadence**: weekly via `asb-people-sync-weekly` scheduler, fires Sunday 06:15 UTC = Saturday 11:15 PM PT (or Sunday 12:15 AM PT in winter). Aligned with the Sunday 8 AM PT `weekly-review-reflect` routine, so the Sunday review sees fresh frontmatter state. Manual on-demand runs via the `sync_people()` MCP tool (§6) or `gcloud run jobs execute`.
- **Idempotency**: re-running the same Airtable state is a no-op. Frontmatter is regenerated and diffed; only differing keys cause a Drive rewrite. The `synced_at` field is the only key that always changes; it's updated only when **other** keys also changed, so a no-op tick doesn't bump it.
- **Body is never touched after first creation.** Once the file exists, frontmatter sync only edits the YAML block; everything below stays untouched, including the `<!-- AUTO -->` sections (those are populated by Phase 2 enricher writes that target the section by header, not by replacing the whole body).
- **Additive only**: when a Contact or Account is deleted in Airtable, the Brain file is **not deleted**. Frontmatter flips to `status: archived`. The note stays indexed in `agent_outputs.notes`; you can filter queries by status. Matches the ADR 0052 additive-only invariant.
- **HIPAA opt-out**: any Account with `Accounts.HIPAA = true` AND any Contact whose primary Account has `hipaa = true` is **excluded entirely**. No Brain note, no sync, no leakage path. Matches CLAUDE.md "Safety rails" + the deferred-HIPAA posture documented in ADR 0055.
- **Slug collisions**: see §1.

### §5 — Body section auto-population (Phases 2 + 4)

The body has four `<!-- AUTO -->` sections that the sync enriches in-place without disturbing user prose elsewhere. Section matching is by exact H2 header.

| Section | Source query | Phase |
|---|---|---|
| `## Active engagements` (Accounts only) | `airtable_replica.projects WHERE account_id = X AND status IN ('Active','Blocked')` | 2 |
| `## Recent activity` (Accounts) | UNION of `triaged_items` mentioning the account (last 30d) + `calendar_events` with attendees in the account's contacts | 2 |
| `## Open risks` (Accounts) | `agent_outputs.risk_flags WHERE account_name = X AND resolved_at IS NULL` | 2 |
| `## Conversation log` (Contacts) | UNION of `triaged_items` WHERE sender_email = contact.email, `calendar_events` WHERE this contact is an attendee, capture_notes that wikilink to this person — last 90 days | 4 |

The enricher replaces only the content between the H2 header and the next H2 (or EOF). Idempotent re-renders. User can add commentary BEFORE the auto section's header or AFTER the next H2 without losing it on the next sync.

### §6 — MCP tool surface (Phase 3)

Two new tools in `mcp_server/tools/person.py`:

```python
def person_summary(name_or_email: str) -> dict:
    """Resolve a person across Airtable + Brain notes and return a structured summary.

    Returns:
        {
            "name": str,
            "email": str | None,
            "role": str | None,
            "warmth": str | None,                # hot/warm/cool/cold/new
            "relationship_type": str | None,
            "last_contact": date | None,
            "next_followup": date | None,
            "recent_activity": [                  # last 5 items
                {"date": str, "source": str, "summary": str, "wikilink_to_note": str | None}
            ],
            "open_followups_due": bool,           # next_followup <= today
            "brain_note_url": str | None,         # Drive URL to the .md file
        }
    """


def sync_people() -> dict:
    """Manually trigger the asb-people-sync Cloud Run Job. Useful when the user has
    just edited Airtable contacts and wants Brain notes refreshed without waiting
    for Sunday's scheduled run.

    Shells to `gcloud run jobs execute asb-people-sync --region=us-central1 --wait`
    via subprocess; requires the local user's ADC to have run.developer or invoke
    perms on the Job (owner@example.com is owner, so this works).

    Returns:
        {"execution_name": str, "succeeded": bool, "log_url": str}
    """
```

`person_summary` is used by Morning Brief routine in place of (or alongside) `client_summary`. Replaces ad-hoc multi-tool plumbing.

`sync_people` is the on-demand trigger — say "sync the people notes" in any Brain MCP-aware session and the Job fires + waits.

### §7 — Phasing (all four ship in one PR per user direction 2026-05-18)

| Phase | Scope | Files | Risk |
|---|---|---|---|
| 1 | Sync stub frontmatter + body skeleton from Airtable replica | `agents/people_sync/main.py`, `frontmatter.py`, `drive_writer.py`, models | Low — additive; never overwrites |
| 2 | Auto-populate Active engagements / Recent activity / Open risks from BQ | `bq_enricher.py`, section-aware writer in `drive_writer.py` | Medium — section detection must be precise |
| 3 | New MCP tools `person_summary` + `sync_people` | `mcp_server/tools/person.py` | Low — additive |
| 4 | Conversation log auto-population | extends `bq_enricher.py` with the contact-side queries | Medium — joining triaged_items by email needs care |

Bundling all four reduces review surface vs. four PRs. Risk is mitigated by tests + the "body never overwritten outside `<!-- AUTO -->` sections" invariant.

### §8 — Service account, IAM, Drive shares

New SA `asb-people-sync-sa@agency-brain-demo.iam.gserviceaccount.com`. Required grants:

- `roles/bigquery.dataViewer` on `airtable_replica` (read accounts, contacts, projects, risk_flags, calendar_events, triaged_items, capture_notes)
- `roles/bigquery.dataEditor` on `agent_audit_log` (emit audit events)
- Custom role `tbPeopleSync` with minimal Cloud Run / logging perms (PRD §4.7 + drafts_boundary_check)
- Drive: fileOrganizer on `Brain/05_GALAXY/01_ACCOUNTS/` and `Brain/05_GALAXY/02_CONTACTS/` (folder-share pattern, ADR 0044). NOT DWD. Folder names mirror the Airtable Operations base table names (Accounts, Contacts) and the user's numbered-prefix Drive convention.

### §9 — What we don't build (yet)

- **Bidirectional sync** (Brain edits → Airtable). The frontmatter is read-only from the Brain side; if user wants to update warmth or next_followup, they do it in Airtable and wait for next sync. Adds conflict-resolution complexity not worth it for v1.
- **Per-person standalone subfolders.** Each person/account is one flat .md file. Sub-files (meeting notes, etc.) live elsewhere (e.g., the account's Solutions Drive folder).
- **Met / Met Context fields**. The Obsidian pattern surfaces these; skipped for v1 since Airtable Contacts doesn't have them as columns. "How I know them" prose section captures the same intent. Easy schema-additive change later.

## Rejected alternatives

- **Hand-curate People/Account notes in Galaxy.** Regression vs. Airtable canonical. New contacts wouldn't auto-land. Warmth/followup updates wouldn't sync.
- **Drive notes as canonical (no Airtable).** Loses cross-team visibility — future hire won't open Drive Docs to update warmth ratings; Airtable is the right shared layer.
- **Synthetic notes only in `agent_outputs.notes` without Drive files.** Works for `brain_ask` but loses the prose surface where the user adds "Who they are" content. The Drive file IS the canvas.
- **Per-relationship-type folders** (`clients/`, `classmates/`, `professors/`, ...). Too granular; relationship type changes (a classmate becomes a hire) would require file moves; better as frontmatter tag.
- **Synthetic per-Account notes only, skip Contacts.** Solves the `[[Client A]]` wikilink case but not the Obsidian-pattern "personal CRM" core use case. Half a solution.

## Related

- ADR 0042 — Personal CRM + Risk Watcher's `PersonalReEngagementSignal` against Airtable contacts. This ADR bridges that structured data to a prose surface.
- ADR 0044 — Drive write via folder-share + ADC. Used by `asb-people-sync-sa`.
- ADR 0052 — Synthetic notes additive-only invariant. Applied here for the deletion semantics.
- ADR 0053 — Wikilink parser + `related_notes`. The wikilink resolution against `notes.filename` is what makes this whole bridge valuable.
- ADR 0054 §2 — Galaxy drop-to-index. The Librarian sweep that picks up these synthetic .md files.
- CLAUDE.md "Safety rails" — HIPAA exclusion invariant.

## Re-architecture checklist (when revisiting)

If a future change wants to add bidirectional sync, add Met/Met_Context fields, or fold this into a more general "atomic notes from any Airtable table" framework:

- [ ] Audit current Airtable schema before adding columns (`bq query 'SELECT column_name FROM \`agency-brain-demo.airtable_replica.INFORMATION_SCHEMA.COLUMNS\` WHERE table_name = "contacts"'`).
- [ ] Schema changes require `airtable/schema.json` bump + image rebuild (CLAUDE.md gotcha).
- [ ] If bidirectional: confirm the Airtable API write scope is set on the sync SA (currently read-only).
- [ ] If generalizing: ensure HIPAA filter logic is centralized — don't reimplement per table.
