"""Commitment extractor — action memory (ADR 0069).

A daily Cloud Run Job that reads recently-ingested ``agent_outputs.notes``
and extracts commitments ("who promised what, by when") into
``agent_outputs.commitments``. One post-processor over the corpus, not
instrumentation across the four ingestion agents — every source already
lands in ``notes`` (ADR 0049 / 0046 / 0031).
"""
