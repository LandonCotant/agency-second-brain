# Memory Bank namespace convention

Every WS-G agent reads and writes through Vertex AI Agent Memory Bank using
the namespace shape defined here. PRD §6.1 mandates this convention; the
helper `agency_brain.common.memory_bank.build_namespace` enforces it.

## Format

```
{agent_id}/{entity_id}[/{subkey}]
```

| Segment | Meaning | Allowed characters |
|---|---|---|
| `agent_id` | Lowercase-hyphenated agent name. Matches the `asb-agent-{name}` resource minus the prefix. | `[a-z0-9][a-z0-9_\-]*` |
| `entity_id` | Natural key the memory is keyed on: client id, project id, user email, or `global` for cross-entity state. | `[a-z0-9][a-z0-9_\-.@]*` |
| `subkey` | Optional second-level key when an agent partitions per-entity state into multiple buckets. | `[a-z0-9][a-z0-9_\-]*` |

`build_namespace` rejects empty segments and disallowed characters. Always
go through it — never concatenate strings inline.

## Why this convention

- **Auditable HIPAA boundary.** A namespace like `risk-watcher/{client_id}/baseline`
  makes it trivial to verify, by listing namespaces, that no entry references
  a HIPAA-excluded client.
- **Owner-attributable.** First segment identifies the agent that wrote the
  entry, mirroring the audit log `agent_id` column.
- **Forward-compatible.** Adding a `subkey` partition (e.g. distinguishing
  baseline vs. cached prompts within the same client scope) doesn't break
  existing readers.

## Initial registry

WS-G PRs append rows here as new namespaces are introduced. **A namespace
that isn't on this table is not approved for use.**

| Pattern | Owner | Contents | Retention | PRD reference |
|---|---|---|---|---|
| `risk-watcher/{client_id}/baseline` | WS-G2 | Rolling 8-week baselines: ROAS, response latency, list size, communication frequency, question-type mix. | Indefinite; updated on every Risk Watcher invocation. | §6.4 |
| `risk-watcher-local/{client_id}/baseline` | WS-G2b | Stakeholder roster, approval turnaround baseline, GBP metric baseline, call attendance pattern. | Indefinite. | §6.2 |
| `risk-watcher-agency/{agency_id}/baseline` | WS-G2c | Report generation/access baseline, refinement-question-to-report ratio (60d), end-client roster, communication initiator pattern. | Indefinite. | §6.3 |
| `triage/{user_email}/cached-classifications` | WS-G1 | Recent classifications keyed by message hash for de-dup across cron retries. | 7 days (caller-managed TTL). | §6.3 |
| `goal-steward/{user_email}/long-form-context` | WS-G6 | Goal-hierarchy steps 1–7 as long-form text — the context the agent uses for classification, not records the agent updates. | Indefinite. | §4 |

## How agents access it

The base class exposes `_mb_read(entity_id, key)` and `_mb_write(entity_id, key, value)`,
which build the namespace as `f"{self.agent_id}/{entity_id}"`. Agents that
need a `subkey` partition call `build_namespace` directly and pass the
resulting namespace to `self._memory_bank.read/write`.

## Adding a namespace

1. Open a PR adding a row to the table above. Include retention, contents,
   and the PRD section that motivates it.
2. The PR review checks: is the entity_id derivable without HIPAA-excluded
   data? Is the contents description specific enough that a future engineer
   can audit the entry without reading the agent code?
3. Once merged, the agent that wrote the row may begin using the namespace.
