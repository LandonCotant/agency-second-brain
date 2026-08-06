# ADR 0044 — Drive write via direct folder share (extends ADR 0031 read pattern)

**Status:** Accepted
**Date:** 2026-05-07
**Workstream:** WS-G PKM Phase 1 follow-up (Reflection-as-Doc + Librarian)

## Context

Two new agent surfaces require Drive **write** access:

1. **Evening Reflection v2 (REFLECT mode, ADR 0040)** stops drafting Gmail
   bodies and instead creates a Google Doc per day at
   `Brain/Areas/Reflections/YYYY-MM-DD-DDD Reflection.gdoc`. The Doc contains
   today's signals, a starter set of standard reflection questions, a few
   LLM-generated custom questions, voice-memo extracts, and an empty section
   the user types into. The Doc is the artifact; a Chat card with a link is
   the notification.
2. **Librarian (next phase)** moves files from `Brain/Inbox/Drop/` into the
   right `Brain/Areas/<topic>/` subfolder after classifying them; it also
   updates a managed "Related" section inside destination dossier docs.

ADR 0031 §1 established the **direct folder-share + ADC** pattern for the
Notes Ingestor's Drive **read** access: the user manually shares each watched
folder with the SA email as Reader; the SA accesses Drive under its own
identity via Application Default Credentials. No Domain-Wide Delegation, no
impersonation, no `dwd_scopes.md` entry. ADR 0027 §2 makes that the
load-bearing posture: **one DWD-grantable SA, scopes `{gmail.compose,
calendar.readonly}`**, period.

This ADR extends the same pattern to **Drive write**.

## Decision

### 1. Drive write goes through ADC + folder share, not DWD

The Reflection writer and Librarian access Drive as **themselves** under
ADC, with the `https://www.googleapis.com/auth/drive` scope requested at
credential build time (lifted from
`notes_ingestor.drive_client.ADCDriveServiceFactory:323`). The user grants
each SA `Editor` on the specific folders the agent operates on:

| SA | Folder | Role | Purpose |
|---|---|---|---|
| `asb-agent-triage-sa` (Reflection runs as) | `Brain/Areas/Reflections/` | Editor | create daily Doc |
| `asb-librarian-sa` (new, Phase D) | `Brain/Inbox/Drop/` | Editor | list + move source files |
| `asb-librarian-sa` | `Brain/Inbox/QuickNotes/` | Editor | optional age-based sweep |
| `asb-librarian-sa` | `Brain/Areas/` | Editor | move destinations + edit dossier `## Related` |

`asb-agent-triage-sa` already holds DWD for `gmail.compose` +
`calendar.readonly` (ADR 0027/0029). Adding **a Drive folder share** does
not change its DWD allowlist — DWD is the impersonation grant; folder
share is plain ACL on the user's Drive. The two are orthogonal.

### 2. ADR 0027 §2 invariant preserved

`dwd_scopes.md` adds **no new rows.** `drafts_boundary_check.py`
`_ALLOWED_DWD_SCOPES` is **untouched.** No SA gains DWD for Drive.

The Reflection writer's Drive write is *non-DWD*: it acts as the SA, not
as the user. The created Doc is owned by the SA, then transferred to the
user's Drive via the parent-folder share (the user's Editor share on the
folder makes the user a contextual editor; Drive's UX shows the Doc in
their own Drive). The user retains full ownership of the file content
because the SA's owner principal is sandboxed by the folder share grant.

### 3. Audit posture

Every Drive write is logged to `agent_audit_log.events` with the
audit-row pattern Notes Ingestor uses. Specifically:

- **Reflection Doc create:** audit `output` includes `{reflection_doc_id,
  reflection_doc_url, parent_folder_id}` so misroutes are queryable.
- **Librarian move:** audit `output` includes `{file_id, from_folder,
  to_folder, confidence, neighbors_linked, related_section_updated}`.
- **Librarian dossier edit:** part of the same audit row; the
  `related_section_updated` boolean records whether
  `dossier_section_editor` ran a `batchUpdate` on the destination.

### 4. HIPAA isolation

`Brain/Inbox/HIPAA/` is **never** shared with `asb-librarian-sa`. The TF
that wires the Librarian's env-var allowlist deliberately excludes the
HIPAA folder ID; the lister's
`_FORBIDDEN_FOLDER_ROLES = {NoteFolder.HIPAA}` provides a code-side guard
even if env vars regress. Drive ACL fails closed: with no folder share,
listing throws 404; even an env-var leak cannot exfiltrate a file the SA
cannot see.

The Reflection writer touches only `Brain/Areas/Reflections/` (a non-HIPAA
folder by convention). Per PRD §4.1 the cascade is honored at the source:
HIPAA-flagged inputs short-circuit at the agent's BaseAgent guard before
any Doc is composed.

### 5. Why not `drive.file` DWD scope

`drive.file` (only files the app creates/opens) is narrower than `drive`
and would technically satisfy ADR 0027 §2's spirit (narrow scope) — but
it has two problems:

- **It still requires DWD.** That means amending `dwd_scopes.md`,
  expanding `drafts_boundary_check.py`, and rerunning the runtime
  drafts-boundary audit job. ADR 0027 §2's invariant is "one
  DWD-grantable SA, two scopes" — three scopes is a meaningful expansion.
- **It can't move user-uploaded files.** Librarian needs to move files
  the user dropped into `Brain/Inbox/Drop/` — files the SA didn't create.
  `drive.file` doesn't grant access to those.

`drive` (full scope) under DWD has wider blast radius and the same
auditing burden. **Folder share + ADC** sidesteps both: the SA's Drive
access is bounded by the user's manual share, not by an OAuth scope, and
the audit story is "what was shared with the SA."

### 6. Markdown → Google Docs rendering

The Reflection writer composes its body as **HTML** and uploads via
`drive.files.create` with `mimeType=application/vnd.google-apps.document`.
Drive auto-converts the HTML on upload — full styling (headings, bold,
bullets) lands without Docs API `batchUpdate` plumbing.

Rejected alternatives:

- **Plain text via `documents.create` + `documents.batchUpdate`.**
  Simplest API surface but loses headings/bold/lists; Doc reads as a wall
  of text.
- **Markdown parsing → `batchUpdate`.** ~150 LoC of fragile parsing for
  the same result HTML auto-convert produces in one API call.

### 7. Deferred — Drive push notifications

Phase A relies on a daily Notes Ingestor tick to ingest the Reflection
Doc into `agent_outputs.notes` for next-day RAG. Drive `changes.watch`
push notifications would shrink this latency to near-real-time, but the
infra cost (webhook endpoint, auth, expiry rotation) is not worth it for
a 2-person tool. Reconsider when notes-corpus volume forces more frequent
ticks.

## Risks

1. **Folder-share latency.** New shares can take ~1 min to propagate;
   document in the runbook.
2. **ADC scope at credential build.** `common/drive_doc_writer.py` MUST
   request `https://www.googleapis.com/auth/drive` at
   `google.auth.default(scopes=[...])` build time. Forgetting fails 403
   at first Doc create. Lift `ADCDriveServiceFactory` directly.
3. **Drive ACL race on first run.** If the user shares the folder *after*
   the Cloud Run Job is updated, the first scheduled tick fails 404. The
   runbook step is "share folder → smoke-fire → verify → unpause
   scheduler."
4. **Librarian → wrong folder.** Confidence threshold (0.6 default) and
   `_uncategorized/` fallback prevent silent misfile. Drive activity
   sidebar shows the move; audit row records `from_folder` + `to_folder`.

## References

- ADR 0027 (DWD delegation surface; the invariant this ADR extends without violating)
- ADR 0031 §1 (Notes Ingestor folder-share + ADC pattern; precedent)
- ADR 0040 (Evening Reflection v2; the upstream consumer for Reflection-as-Doc)
- ADR 0038 (`VECTOR_SEARCH` infra; Phase C's reader queries this)
- PRD §4.1 (HIPAA isolation; the cascade this ADR's Librarian must respect)
