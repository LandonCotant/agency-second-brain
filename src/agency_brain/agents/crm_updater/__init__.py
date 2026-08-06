"""CRM Auto-updater — drafts Airtable updates from `secondbrain`-labeled Gmail.

Per ADR 0047:
- Trigger: daily Cloud Run Job at 06:15 PT.
- Source: Gmail messages with `secondbrain` label, since last successful run.
- Read scope: ``gmail.readonly`` + ``gmail.modify`` on ``asb-agent-triage-sa``
  (DWD allowlist expansion).
- Output: Airtable drafts — Tasks (`Approval Status = "Drafted by Agent"`)
  and ``Pending Updates`` long-text blocks on Contacts + Accounts.
- Drafts-only per PRD §4.7; humans approve every change.
"""
