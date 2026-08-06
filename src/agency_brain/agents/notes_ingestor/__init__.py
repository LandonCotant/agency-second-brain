"""Samsung Notes ingestor (ADR 0031).

Cloud Run Job that polls two Drive folders shared with
``asb-notes-ingestor-sa``:

  Brain Inbox/Notes/        → aspects=["samsung_note"]
  Brain Inbox/Notes-HIPAA/  → aspects=["samsung_note","hipaa_excluded"]

For each new PDF (filtered by Drive ``modifiedTime > watermark``):
extract Markdown + image captions via Vertex ``gemini-2.5-flash``
multimodal, write to ``agent_outputs.notes``, publish to
``asb-triage-input`` so the existing Triage Reasoning Engine can
classify.

Pattern mirrors the Morning Brief topology (ADR 0029): Cloud Run Job
+ scheduler + Vertex SDK direct (NOT a Reasoning Engine, per ADR
0028). Does not subclass BaseAgent — emits its own audit rows
directly via ``AuditLogClient.emit()`` with ``agent_id="notes-ingestor"``.
"""
