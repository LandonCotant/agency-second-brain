# Observability tuning + alert setup runbook

Operational guide for the alerts owned by [`terraform/modules/observability/`](../../terraform/modules/observability/).
PR 1 ships the email + Chat channels and the HIPAA isolation alert; this
runbook covers their one-time setup, smoke testing, and tuning.

## One-time: provision the Chat space + add Cloud Monitoring app

The Cloud Monitoring → Chat path uses the native `google_chat` channel
type via the Google Cloud Monitoring Chat app — see
[ADR 0008](../adr/0008-chat-alerting-via-google-chat-channel.md) (and ADR 0007,
superseded, for what we tried first). Setup is two manual clicks:

1. **Create the Chat space.**
   - Open Google Chat as `owner@example.com`.
   - "+ New chat" → "Create space" → name it `Brain alerts`.
   - Members: just yourself for now. Set "Conversation history: ON" so
     historical alerts remain searchable.
   - Note the space ID from the URL (the part after `/space/`, e.g.
     `AAAAEXAMPLESPACE`). Add it to `terraform/envs/prod/terraform.tfvars` as
     `chat_space_id`.

2. **Add the Google Cloud Monitoring app to the space.**
   - In the Brain alerts space, click the space name at the top.
   - "Apps & integrations" → "+ Add apps".
   - Search **Google Cloud Monitoring** → click → "Add to space".
   - The app appears as a member of the space; Cloud Monitoring can now
     post via it.

3. **(First apply only)** `terraform apply` from `terraform/envs/prod/`.
   Cloud Monitoring sends a verification email to `owner@example.com`
   — click the link or the email channel won't deliver. The Chat channel
   is live the moment apply completes; no separate verification.

## Smoke test the HIPAA alert

After apply, fire a synthetic event from your shell:

```bash
gcloud logging write asb-hipaa-test \
  '{"event":"HIPAA_GUARD_TRIPPED","note":"runbook smoke test"}' \
  --severity=ERROR --payload-type=json \
  --project=agency-brain-demo
```

Expected timeline:
- Within ~60s: log entry appears in Cloud Logging.
- Within ~2–3 min: log-based metric `asb-hipaa-guard-tripped` increments.
- Within ~3–5 min: alert policy `HIPAA isolation breach (P0)` opens an
  incident; the Chat space receives a message linking back to the incident.

Resolve the test incident in Cloud Monitoring → Alerting → Incidents (it
auto-closes after 7 days otherwise).

If nothing fires:
- Verify the metric exists: `gcloud logging metrics describe asb-hipaa-guard-tripped --project=agency-brain-demo`
- Check whether the synthetic log line actually matched the filter:
  Cloud Logging → Logs Explorer → run the filter from `alerts_hipaa.tf`.
- Verify the Cloud Monitoring app is actually a member of the Chat space:
  open the space → space name → "Manage members" → confirm "Google Cloud Monitoring" is listed.
- Confirm the channel is wired to the policy:
  `gcloud alpha monitoring policies list --project=agency-brain-demo --format='value(notificationChannels)'`

## Tuning

The PR 1 filter is intentionally broad
(`HIPAA_GUARD_TRIPPED` token in any payload field, all resource types). Once
real agents (WS-C base class, WS-F audit script) start emitting in PR 2+,
narrow the filter to known emitters to reduce surface for accidental
false positives:

```
resource.type=("aiplatform.googleapis.com/ReasoningEngine" OR "cloud_function")
  AND (jsonPayload.event="HIPAA_GUARD_TRIPPED")
```

Don't lower the threshold below 1 — PRD §8.2 row 1 is "≥ 1 event". If a real
breach occurs and the alert produces noise during incident response, silence
via Monitoring UI for the duration of the incident only; never disable in
Terraform.

## Halt-execution kill-switch (cross-workstream)

The HIPAA alert message instructs the on-call (the operator) to flip a
`agent-kill-switch` Secret Manager flag. WS-F owns:
- creating the secret
- wiring agents to read it on each invocation
- the automated halt path

Until WS-F merges, the manual command in the alert documentation is the
only halt mechanism. Audit the WS-F PR when it lands and update the alert
documentation if the secret name or command changes.

## Future alerts (PR 2)

The four remaining §8.2 alerts (sync failure, agent error rate, audit log
write failure, Model Armor spike) ship after WS-C's `agent_audit_log.events`
schema merges to main. They reuse the channel IDs exported from this module
(`module.observability.email_channel_id`, `module.observability.chat_channel_id`).
The cost-anomaly alert is week 9–10 work.
