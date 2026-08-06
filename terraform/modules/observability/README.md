# observability — Dashboards, alerts, audit log table (WS-E)

Owns the application-layer audit log (`agent_audit_log.events`) and all custom monitoring beyond what Agent Platform provides natively (traces + cost are built-in via Agent Observability).

See [acceptance criteria](../../../docs/acceptance/ws-e-observability.md) for the full scope.

## Status

| Area | PR shipping | Resources |
|---|---|---|
| Email + Chat notification channels | **PR 1 (this PR)** | `notifications.tf` |
| HIPAA isolation alert (P0) on Cloud Audit Logs | **PR 1 (this PR)** | `alerts_hipaa.tf` |
| `agent_audit_log.events` BigQuery table + IAM | PR 2 (after WS-C schema lands in main) | — |
| Sync failure alert | PR 2 | — |
| Agent error rate alert | PR 2 | — |
| Audit log write failure alert | PR 2 | — |
| Model Armor block-rate spike alert | PR 2 | — |
| §8.1 dashboards (classification, sync, cost, security) | PR 2 | — |
| Cost-anomaly alert | Week 9–10 (needs baseline) | — |

## What PR 1 ships

- `email_owner` — `google_monitoring_notification_channel` of type `email`, addressed to the project owner.
- `chat_brain_alerts` — `google_monitoring_notification_channel` of type **`google_chat`**. Posts via the Google Cloud Monitoring Chat app installed in the Brain alerts space. Space ID passed as `var.chat_space_id`. See [the runbook](../../../docs/runbooks/observability_tuning.md) for one-time setup. See [ADR 0008](../../../docs/adr/0008-chat-alerting-via-google-chat-channel.md) (and ADR 0007, superseded) for why this design.
- `hipaa_guard_tripped` — `google_logging_metric` counting `HIPAA_GUARD_TRIPPED` log entries across all payload fields.
- `hipaa_isolation_breach` — `google_monitoring_alert_policy` firing on any nonzero count over 5 minutes; routes to the Chat channel only (P0 — no email noise).

## Operator setup (one-time, before first apply)

1. Create the Chat space (see runbook). Note the space ID and put it in `terraform/envs/prod/terraform.tfvars` as `chat_space_id`.
2. Add the **Google Cloud Monitoring** app to the space: space name → Apps & integrations → + Add apps → search "Google Cloud Monitoring" → Add to space.
3. Run `terraform apply` from `terraform/envs/prod/`.
4. Verify the email notification by clicking the confirmation link Cloud Monitoring sends to the owner address. Without verification, the email channel exists but won't deliver. The Chat channel is live the moment apply completes — no extra step.

## Smoke test

After apply, fire a synthetic HIPAA event:

```
gcloud logging write asb-hipaa-test \
  '{"event":"HIPAA_GUARD_TRIPPED","note":"smoke test"}' \
  --severity=ERROR --payload-type=json --project=agency-brain-demo
```

Within ~3 minutes the metric increments and the Chat channel fires. Resolve the incident in the Monitoring UI.

## Dependencies

- `module.foundation.brain_project_id` — every resource is project-scoped to it.
- WS-C base agent class (PR 2) — emits `HIPAA_GUARD_TRIPPED` on pre-flight failure; the metric filter expects that token.
- WS-F `audit/hipaa_isolation_check.py` (separate workstream) — emits the same token from the hourly cron.
