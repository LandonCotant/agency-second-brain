"""Calendar ingester — daily Cloud Run Job that pulls Google Calendar
events into ``agent_outputs.notes`` for VECTOR_SEARCH retrieval (ADR 0046).

Per ADR 0046 §2 (Workstream A v1):
- Reuses existing DWD ``calendar.readonly`` scope (ADR 0027 §2; no new scope).
- Impersonates ``asb-agent-triage-sa`` (the lone DWD-grantable SA per ADR 0027 §3).
- Default lookback 180 days back, 90 days forward; cron 06:30 PT daily.
- HIPAA filter: drops events whose attendees include a HIPAA-flagged
  client domain (PRD §4.1 layer 3).
- Writes to the same ``agent_outputs.notes`` table as Drive notes, with
  ``note_kind = 'calendar_event'`` and ``event_metadata`` populated.
- Idempotent re-runs via MERGE on ``external_id = event.id``.
"""
