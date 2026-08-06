# ADR 0048 — the agency Shared Drive ingestion

**Status:** Accepted — 2026-05-11 (Phase H, WS-G corpus expansion).
Extends ADR 0031 / 0037 (Notes Ingestor) and ADR 0044 / 0045 (Drive write architecture).

## Context

The Knowledge Surfacer `/ask` retrieves from `agent_outputs.notes`, but
the corpus is sparse:

- Notes Ingestor only walks Brain Inbox folders + `Brain/Areas` (ADR 0031 / 0037).
- Librarian only acts on files dropped into `01_BRAIN_INBOX/06_DROP/` (ADR 0044).
- Calendar ingester writes calendar events, CRM Auto-updater drafts
  Airtable Tasks. Neither ingests the existing **the agency**
  Shared Drive content.

All client materials (meeting notes, strategy docs, onboarding intake,
campaigns, reporting) and internal agency knowledge (sales playbooks,
ops SOPs, finance, legal) accumulate in the Solutions Shared Drive and
are invisible to `/ask`.

The user explicitly requested broader ingestion + client-folder sweep
(plan `determine-the-next-steps-curried-rivest.md`, 2026-05-11).

## Decision

Extend the existing **Notes Ingestor** Cloud Run Job (do NOT build a new
agent) with two new folder roles and a discovery step at tick start.

### §1 — Two new `NoteFolder` roles

```
NoteFolder.SOLUTIONS_CLIENT     # 05_CLIENTS/<client>/<allowlisted-subfolder>/
NoteFolder.SOLUTIONS_INTERNAL   # 01_MGMT, 02_FIN, 03_OPS, 04_SALES — recursive
```

Both map to `note_kind = AREA`, `scope = AGENCY`. Neither publishes to
the triage Pub/Sub topic (Solutions content is reference material for
`/ask`, not actionable inbox items).

### §2 — Recursive walker for internal Solutions folders

Four env vars carry the four top-level Solutions roots. Each becomes a
single `FolderConfig(role=SOLUTIONS_INTERNAL, recursive=True)`. The
drive client's BFS walker visits every descendant; the per-root watermark
advances to the highest `modifiedTime` seen across the subtree.

```
SOLUTIONS_MANAGEMENT_LEGAL_FOLDER_ID   → 01_MANAGEMENT & LEGAL/
SOLUTIONS_FINANCE_FOLDER_ID            → 02_FINANCE & ACCOUNTING/
SOLUTIONS_OPERATIONS_HR_FOLDER_ID      → 03_OPERATIONS & HR/
SOLUTIONS_SALES_MARKETING_FOLDER_ID    → 04_SALES & MARKETING (Internal)/
```

No subfolder filtering — the user explicitly opted in to ingesting all
four roots in full. They are the only operator of this single-tenant
tool and `/ask` is email-allowlisted, so broader corpus is acceptable.

### §3 — Discovery + allowlist for client folders

`SOLUTIONS_CLIENTS_FOLDER_ID` points to `05_CLIENTS/`. At each tick the
ingester:

1. Lists immediate children (one folder per client).
2. Skips `00_CLIENT_TEMPLATE` (template skeleton).
3. Normalizes each remaining folder name and looks it up against
   `airtable_replica.accounts` where `hipaa = TRUE`. If matched → the
   client folder is skipped entirely (no listing, no download, no
   embedding — defense in depth per the project memory
   `project_hipaa_deferred.md`).
4. For each remaining client, lists immediate subfolders and keeps only
   those whose names match the allowlist:

   ```
   00_ONBOARDING
   01_STRATEGY
   05_CAMPAIGNS_AND_CHANNELS
   07_REPORTING (EXTERNAL)
   08_MEETING_NOTES
   ```

   Skipped subfolders (per user spec, 2026-05-11):
   `02_LEGAL_ADMIN`, `03_CLIENT_BRAND_LIBRARY`, `06_DELIVERABLES`,
   `09_AGENT_WORKSPACE`. Reason: non-text content (brand assets, final
   deliverable files) or canonical-in-Airtable material (contracts,
   agent staging).

5. Yields one recursive `FolderConfig(role=SOLUTIONS_CLIENT,
   client_name=..., source_path=..., recursive=True)` per matched
   subfolder. Recursion is enabled because users may organize deeper
   inside (e.g., `08_MEETING_NOTES/2026Q1/`).

The folder-name normalizer (`solutions_discovery.normalize_folder_name`)
strips a leading `\d+[_\s]+` prefix, collapses runs of `_`, replaces `_`
with space, and case-folds. Test coverage in
`tests/unit/agents/notes_ingestor/test_solutions_discovery.py`.

### §4 — Reference files stay where they are

The existing inbox model moves files to a `processed/` subfolder after
a successful BQ write (so the next tick doesn't re-list them). Solutions
files are reference material that lives in the user's library; moving
them out of place would break their daily workflow. The dedup key
(`source_drive_file_id`, `revision_id`) already prevents re-ingestion.

New helper `models.should_move_to_processed(folder)` returns False for
both Solutions roles; the call site in `main._process_file` gates on it.

### §5 — Source attribution via Markdown header

Solutions files are written to `agent_outputs.notes` with a 1-3 line
header prepended to `markdown_content`:

```
Client: Client A
Source: 05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES/2026-05-01_kickoff.pdf

<original markdown body>
```

For internal folders the `Client:` line is omitted; only `Source:` is
present. The header is prepended **before** embedding so the
`text-embedding-005` vector includes the client tag — `/ask` retrieves
client-tagged content semantically without a schema change.

No new column on `agent_outputs.notes`. If retrieval quality on multiple
clients ever proves insufficient, a follow-up could add a structured
`account_id STRING NULLABLE` column (v1.5).

### §6 — IAM + Drive access

`asb-notes-ingestor-sa` needs:

- **Drive read** on the the agency Shared Drive. Granted as
  **Viewer** at the Shared Drive level (not Content Manager — we don't
  write). One-time manual step per runbook
  `docs/runbooks/notes-ingestor.md` §Solutions Drive access. Precedent:
  `asb-librarian-sa` is Content Manager on the same Shared Drive per
  ADR 0045 §7; we use the lower-privilege role.
- **BQ read** on `airtable_replica` for the HIPAA lookup. Added as
  `roles/bigquery.dataViewer` in `notes_ingestor.tf`.

No new DWD scope. ADR 0027 §2 invariant preserved.

## Consequences

- `/ask` retrieves from client folders + internal agency knowledge after
  the first daily tick (06:00 PT). First-tick burst is bounded by
  `MAX_NOTES_PER_TICK` (default 100) — subsequent ticks chew through the
  rest at ≤100 files/day until caught up.
- HIPAA-flagged accounts: `accounts.hipaa = TRUE` instantly excludes a
  client folder from ingestion. Adding a HIPAA flag retroactively does
  NOT delete already-ingested rows; the operator must DELETE them from
  `agent_outputs.notes` manually. (Acceptable for v1 — no HIPAA accounts
  exist today per the project memory.)
- Folder-name → account-name matching is fuzzy. The first prod tick logs
  non-matches at INFO; the operator can rename folders or add an
  explicit `accounts.drive_folder_id` Airtable field (v1.5) if drift
  becomes painful.
- Slides / Spreadsheets / non-text MIME types are logged-and-skipped by
  the existing MIME allowlist (PDF, Google Docs, Markdown, audio).
  Broaden in a follow-up if reporting decks become important.
- `asb-notes-ingestor-sa` gains read access to internal Solutions
  folders. Acceptable because (a) this is a single-operator tool, (b)
  the SA itself has no DWD and no Gmail-send capability, (c) `/ask` is
  email-allowlisted, so even an exploit chain ending in `/ask` only
  exposes content to the operator.

## Out of scope

- **Per-client retrieval scoping in `/ask`** (e.g., "only retrieve from
  Client A folder"). v1 relies on semantic match via the prepended
  `Client:` header. Add a structured filter in a follow-up if needed.
- **Re-ingestion on HIPAA flag flip.** Setting `accounts.hipaa = TRUE`
  on an account whose folder was already ingested does NOT purge the
  existing rows. Manual cleanup required (`DELETE FROM agent_outputs.notes
  WHERE markdown_content LIKE 'Client: <name>%'`).
- **`Agency Second Brain/01_BRAIN_INBOX/05_AREAS` ingestion change.** The
  existing `BRAIN_AREAS_FOLDER_ID` continues to ingest under the
  `AREAS` role + `PERSONAL` scope per ADR 0037. Unchanged.

## Supersedence

This ADR extends ADR 0031 (Notes Ingestor) and ADR 0037 (PKM merge).
Neither is superseded — the new folder roles slot in alongside the
existing ones via the same `_FOLDER_ENV_MAP` + role-mapping pattern.
