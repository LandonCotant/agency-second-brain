# WS-E: Observability — Acceptance Criteria

Sign-off gate for merging the `ws-e-observability` branch. Per [PRD.md](../../PRD.md) §5.4, §8, §7.4.

**Depends on:** WS-A (merged).
**Wall-clock target:** continuous (kickoff in week 1, hardens through week 12).

## Functional checks

- [ ] **Application audit log table.** `agent_audit_log.events` BigQuery table created with schema from [PRD.md](../../PRD.md) §4.6 layer 2: `event_id, timestamp, agent_id, agent_identity_uuid, sa_email, input_summary, output, confidence, latency_ms, cost_usd, hipaa_guard_status, model_armor_findings`. Partitioned by date, clustered by `agent_id`.
- [ ] **Audit log write client.** WS-C's `common/audit_log.py` writes here; integration test verifies a row lands within 5 seconds of an agent invocation.
- [ ] **Custom dashboards** (BigQuery-driven, Cloud Monitoring or Looker Studio):
  - Classification quality (confidence distribution, dismissal vs confirmation rate, false-positive per Risk Profile)
  - Sync health (lag per source, schema-drift events, HIPAA filter exclusions per cycle)
  - Cost (monthly projection vs. PRD §15 budget; supplements Agent Observability built-in)
  - Security (`HIPAA_GUARD_TRIPPED` count must be zero; DWD scope drift; IAM binding changes; Model Armor findings)
- [ ] **Required alerts** wired (PRD §8.2):
  - HIPAA isolation breach ≥ 1 → Chat DM to the operator + halt agent execution
  - Sync flow failure 2 consecutive → Email + Chat
  - Agent error rate > 10% over 1h → Chat
  - Audit log write failure ≥ 1 → Email + halt agent execution
  - Model Armor block rate spike > 5x baseline over 1h → Email
- [ ] **Cost-anomaly alert** added in week 9–10 (NOT week 1 — needs stable baseline first).

## Security checks

- [ ] Alert delivery SAs (e.g. Cloud Monitoring notification channel SAs) are scoped — no broad permissions.
- [ ] Audit log table has restrictive IAM (only the operator + the agent SAs that write to it).

## Documentation checks

- [ ] `terraform/modules/observability/README.md` lists every dashboard and alert.
- [ ] Runbook for tuning alert thresholds at `docs/runbooks/observability_tuning.md`.
- [ ] At least one ADR if any non-obvious decision (dashboard tooling choice, alert routing).

## Sign-off

- [ ] the implementer — _____________________________ — date: __________
