"""WS-G4 Evening Reflection Composer (ADR 0036).

Backward-looking reflective artifact drafted into owner@'s mailbox
each evening at 18:00 PT. Mirrors the WS-G3 Morning Brief topology:
Cloud Run Job + daily scheduler + Vertex SDK direct + DWD
``gmail.compose``+``calendar.readonly`` on ``asb-agent-triage-sa`` +
per-recipient-per-local-date dedup + always-draft fallback.
"""
