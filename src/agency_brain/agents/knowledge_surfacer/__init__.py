"""Knowledge Surfacer retrieval library — VECTOR_SEARCH over the notes corpus.

Originally a full Cloud Run service exposing a ``/ask`` Chat slash command +
``/api/ask`` route (ADR 0046, ADR 0050). **The service was retired per
ADR 0059** — it had zero usage and no internal callers once the MCP server
(ADR 0051) made the Claude app the canonical query surface.

What remains is the retrieval primitive only:
- ``retriever.Retriever`` — BQ ``VECTOR_SEARCH`` on ``agent_outputs.notes``
  (ADR 0038), with the ADR 0045 §9 ``ARRAY_LENGTH(embedding) = 768`` and
  ``hipaa_isolated = FALSE`` pre-filters.
- ``models.RetrievedChunk`` — the chunk DTO it returns.

This library is imported in-process by the ``brain_ask`` MCP tool
(``mcp_server/tools/read.py``); the host LLM does the synthesis Claude-side.
No Cloud Run service, no Gemini synthesis step, no Chat/OIDC surface.
"""
