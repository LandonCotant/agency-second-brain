# agents — Agent implementations (WS-C base + WS-G subclasses)

WS-C owns `base.py` and the deployment patterns. WS-G workstreams add
subclasses for individual agents (triage, risk_watcher, goal_steward, brief,
knowledge_surfacer, etc.).

## Base class contract

`base.py:BaseAgent` provides every agent with:

1. **HIPAA pre-flight check** — aborts with `HipaaGuardTripped` on any input
   carrying the `hipaa_excluded` aspect. Audit row emitted with
   `hipaa_guard_status = TRIPPED` *before* the exception is raised.
2. **Audit log emission** — structured row to `agent_audit_log.events` on
   every invocation: success, exception, or HIPAA trip. See
   [ADR 0006](../../../docs/adr/0006-audit-log-streaming-and-emit-on-every-path.md).
3. **Prompt loading** — `_load_prompt(name, version)` reads from
   `prompts/{name}/{version}.md`. No inline prompt strings.
4. **Confidence threshold** — `confidence < 0.7` sets `human_review_routed = True`
   on the audit row. WS-D acts on this signal.
5. **Memory Bank namespace helpers** — `_mb_read` / `_mb_write` build
   namespaces per [docs/memory_bank_namespaces.md](../../../docs/memory_bank_namespaces.md).

Cost tracking, retries, and circuit breakers are NOT in the base class. The
Agent Runtime SDK handles transient retries; Agent Observability tracks cost.
Add custom retry only if a specific agent needs it.

## Subclassing

```python
from agency_brain.agents.base import BaseAgent

class TriageAgent(BaseAgent[TriageInput, TriageOutput]):
    def _run(self, input: TriageInput) -> TriageOutput:
        prompt = self._load_prompt("triage", "v1")
        # ... call Gemini, parse output, return TriageOutput
        return TriageOutput(confidence=..., classification=...)
```

**Do not override `invoke`.** Override `_run` only — the wrapper enforces
the contract above. PR review rejects subclasses that override `invoke`.

Per [PRD.md](../../../PRD.md) §6.1.
