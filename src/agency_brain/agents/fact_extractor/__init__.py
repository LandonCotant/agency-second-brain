"""Fact extractor — bi-temporal factual memory (ADR 0070).

A daily Cloud Run Job that reads recently-ingested ``agent_outputs.notes``
and extracts entity-attribute facts ("Acme · retainer · $3k") into the
append-only ``agent_outputs.facts`` log. Current-vs-superseded validity is
derived at read time (latest ``observed_date`` per ``(entity, predicate)``
wins) — no UPDATE, no streaming-buffer DML. Mirrors the ADR 0069
commitment extractor: one post-processor over the corpus.
"""
