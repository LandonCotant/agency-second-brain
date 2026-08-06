# common — Shared agent infrastructure (WS-C)

Shared utilities every agent imports. WS-C owns the conventions; other
workstreams import from here without modifying.

| Module | Purpose |
|---|---|
| `models.py` | `AuditEvent` dataclass, `HipaaGuardStatus` enum, `AgentInput` / `AgentOutput` Protocols. Mirrors the `agent_audit_log.events` BQ schema. |
| `audit_log.py` | `AuditLogClient` — only sanctioned writer for `agent_audit_log.events`. Streaming inserts; raises `AuditLogWriteError` on persistent failure. |
| `memory_bank.py` | `MemoryBank` Protocol, `InMemoryMemoryBank` (tests/dev), `VertexMemoryBank` (prod), `build_namespace` enforcing the `{agent_id}/{entity_id}/{key}` convention. |
| `prompts.py` | `load_prompt(name, version)` — reads `prompts/{name}/{version}.md`. Never inline. |

**Roadmap (this workstream's later PRs):**

- PR #2 will add `cached_context.py` (Cached Contents helper) and wire the
  Vertex Memory Bank instance so `VertexMemoryBank.read/write` are
  exercised end-to-end.
- ~~**PR #3 — BLOCKING for downstream workstreams.**~~ **Shipped.** Added
  the `agent_outputs.*` BQ table schemas (`triaged_items`, `risk_flags`,
  `goals`, `goal_scores`) in `terraform/modules/agent_runtime/main.tf`.
  Direct `bq.insert_rows_json` is sufficient — no client wrappers
  needed. WS-B PR-2 (sync writes) and WS-D (routing reads) are now
  unblocked. Schema discipline documented in
  [ADR 0009](../../../docs/adr/0009-agent-outputs-schema.md).

Per [PRD.md](../../../PRD.md) §6.1, the base agent class composes these.
See [ADR 0006](../../../docs/adr/0006-audit-log-streaming-and-emit-on-every-path.md)
for the audit-log emission posture.
