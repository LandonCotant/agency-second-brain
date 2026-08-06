# Runbook — Routing fan-out setup (Chat lane)

Manual operator steps to bring up the WS-D Chat fan-out worker
(`asb-routing-fanout`). Architecture and rationale: `docs/adr/0023-routing-chat-fanout.md`.

## Prerequisites

- PR 1 (code scaffolding) merged to `main`.
- PR 2 (this PR — TF) ready to apply.
- You are owner@example.com (or have admin access to the
  `Brain alerts` Google Chat space and the `agency-brain-demo`
  GCP project).

## One-time setup

### 1. Create an incoming webhook in the Brain alerts space

Operator action — Workspace UI, can't be done via Terraform.

1. Open Google Chat → `Brain alerts` space.
2. Space header → **Manage webhooks** → **Add webhook**.
3. Name: `Brain routing fan-out`. Avatar: optional.
4. Click **Save** and copy the URL. It looks like:
   `https://chat.googleapis.com/v1/spaces/AAAA.../messages?key=...&token=...`
5. Treat the URL as a secret. Do not paste it into the repo, into a PR
   description, or into a chat session. Anyone with the URL can post
   to the space.

### 2. Build + push a placeholder image at `:bootstrap`

The TF declares the Cloud Run Job at the `bootstrap` tag. The first
apply fails unless that tag exists.

```bash
# From repo root, on the PR 2 branch:
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build --platform=linux/amd64 \
  -f Dockerfile.routing-fanout \
  -t us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/routing-fanout:bootstrap \
  .
docker push us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/routing-fanout:bootstrap
```

If `Dockerfile.routing-fanout` does not exist yet (it lands in PR 3), use
the existing `Dockerfile.triage-bridge` as a temporary placeholder — the
container won't run anything useful, but the apply will succeed and the
real image will replace it in PR 3.

### 3. Targeted Terraform apply

Per `feedback_prod_touching_workflow.md`: targeted, paused, no destroys.

```bash
cd terraform/envs/prod

terraform plan -no-color \
  -target=module.agent_runtime.google_service_account.tb_routing_sa \
  -target=module.agent_runtime.google_project_iam_custom_role.tb_routing_fanout \
  -target=module.agent_runtime.google_project_iam_member.tb_routing_fanout_role \
  -target=module.agent_runtime.google_bigquery_dataset_iam_member.tb_routing_outputs_editor \
  -target=module.agent_runtime.google_bigquery_dataset_iam_member.tb_routing_audit_writer \
  -target=module.agent_runtime.google_secret_manager_secret.brain_alerts_chat_webhook \
  -target=module.agent_runtime.google_secret_manager_secret_iam_member.brain_alerts_chat_webhook_accessor \
  -target=module.agent_runtime.google_service_account.tb_routing_fanout_invoker_sa \
  -target=module.agent_runtime.google_cloud_run_v2_job.tb_routing_fanout \
  -target=module.agent_runtime.google_cloud_run_v2_job_iam_member.routing_fanout_scheduler_invoker \
  -target=module.agent_runtime.google_cloud_scheduler_job.tb_routing_fanout_5m
```

Expected: ~10 `to add`, 0 `to change`, 0 `to destroy`. **If anything is
in the destroy list, stop and triage** — there is no destructive change
in this PR.

After confirming, apply with the same `-target=` set.

### 4. Add the webhook URL as a secret version

```bash
printf '%s' '<paste-webhook-url-here>' | \
  gcloud secrets versions add second-brain-gchat-webhook \
    --data-file=- \
    --project=agency-brain-demo
```

Verify the version:

```bash
gcloud secrets versions list second-brain-gchat-webhook \
  --project=agency-brain-demo
```

### 5. Disable the scheduler until PR 3's image is live

PR 2's Cloud Run Job runs the placeholder (or triage-bridge) image,
which would either no-op or error out every 5 min. Pause the scheduler
until PR 3 ships the real entrypoint:

```bash
gcloud scheduler jobs pause asb-routing-fanout-5m \
  --location=us-central1 \
  --project=agency-brain-demo
```

PR 3's smoke-test step resumes it.

## Verification (after PR 3)

1. **Webhook reachable.** From any shell with the URL handy:
   ```bash
   curl -fsSL -H 'Content-Type: application/json' -d '{"text":"smoke from setup runbook"}' "$WEBHOOK_URL"
   ```
   A new line should appear in the Brain alerts space.
2. **TF state.** `terraform plan` clean.
3. **Secret accessor.** `gcloud secrets get-iam-policy second-brain-gchat-webhook --project=agency-brain-demo` shows exactly one binding for `asb-routing-sa@...`.
4. **Cloud Run Job present.** `gcloud run jobs list --region=us-central1 --project=agency-brain-demo | grep asb-routing-fanout`.
5. **Scheduler exists, paused.** `gcloud scheduler jobs describe asb-routing-fanout-5m --location=us-central1 --project=agency-brain-demo | grep state`.

## Rotation

If the webhook URL leaks:

1. Revoke the webhook in Google Chat (space → Manage webhooks → Delete).
2. Create a fresh webhook with the same name (step 1 above).
3. Add the new URL as a new secret version (step 4 above) — Cloud Run
   reads `latest` on every job invocation, so the next scheduler tick
   picks up the new URL.

## Adding a channel: Gmail draft (ADR 0032)

After Chat is live, the second channel is Gmail drafts. Code lives in
`src/agency_brain/routing/channels/gmail.py`; matrix entries land in
`_MATRIX["critical"]` and `_MATRIX["high"]`. The Cloud Run Job
(`asb-routing-fanout`), scheduler, image, and SA are unchanged — the
multi-channel agent dispatches every matrix-routed channel that has a
registered adapter.

### Prerequisites

- ADR 0027 + 0029 DWD allowlist (`gmail.compose`, `calendar.readonly`)
  already in place on `asb-agent-triage-sa`. Verify with the
  `asb-audit-drafts-boundary` job's most recent log line — it must
  emit `dwd_scopes_checked: 2`.
- PR 1 (#68) merged: code + ADR 0032.
- PR 2 (#69) merged: TF (the
  `serviceAccountTokenCreator` binding + env vars).
- PR 3 (this section): permanent cloudbuild config + image + smoke.

### 1. Targeted Terraform apply for PR 2

Per `feedback_prod_touching_workflow.md`:

```bash
cd terraform/envs/prod
terraform plan -no-color \
  -target=module.agent_runtime.google_service_account_iam_member.tb_routing_can_impersonate_triage_for_dwd \
  -target=module.agent_runtime.google_cloud_run_v2_job.tb_routing_fanout
```

Expected: 1 to add (the `serviceAccountTokenCreator` binding), 1 to
change (the Cloud Run Job's env-var diff for `TRIAGE_SA_EMAIL` +
`GMAIL_DRAFT_RECIPIENT`), 0 to destroy. Apply with the same
`-target=` set.

### 2. Build + push the routing-fanout image

```bash
gcloud builds submit \
  --project=agency-brain-demo \
  --region=us-central1 \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=cloudbuild.routing-fanout.yaml \
  --substitutions=_TAG=adr-0032-gmail-draft-v1 \
  .
```

Verify the image landed in AR:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/routing-fanout \
  --filter="tags=adr-0032-gmail-draft-v1" \
  --project=agency-brain-demo
```

### 3. Roll the Cloud Run Job to the new image

```bash
gcloud run jobs update asb-routing-fanout \
  --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/routing-fanout:adr-0032-gmail-draft-v1 \
  --region=us-central1 \
  --project=agency-brain-demo
```

### 4. Force-fire one execution + tail the log

```bash
gcloud run jobs execute asb-routing-fanout \
  --region=us-central1 \
  --project=agency-brain-demo \
  --wait
```

Expected log lines: `fanout tick start: ... channels=['gmail_draft', 'google_chat_dm']`
and `fanout tick done: polled=N dispatched=M ...`.

### 5. End-to-end smoke

On a quiet hour (so the only critical signal in the lookback is the
synthetic):

1. **Publish a synthetic critical Gmail signal** to `asb-triage-input`.
   Reuse the Client-A fixture from the 2026-05-01 Chat smoke.
2. Wait ≤10 min (one bridge tick + one fan-out tick).
3. **Verify the triage row landed.**
   ```bash
   bq query --use_legacy_sql=false --project_id=agency-brain-demo \
     "SELECT item_id, severity, source FROM agent_outputs.triaged_items ORDER BY triaged_at DESC LIMIT 1"
   ```
4. **Verify TWO routed_events rows** (one per channel):
   ```bash
   bq query --use_legacy_sql=false --project_id=agency-brain-demo \
     "SELECT item_id, channel, routed_at, chat_status FROM agent_outputs.routed_events WHERE item_id = '<from step 3>'"
   ```
   Expected: rows with `channel='google_chat_dm'` (chat_status=200)
   AND `channel='gmail_draft'` (chat_status NULL).
5. **Verify the Chat card** appeared in the `Brain alerts` space.
6. **Verify the Gmail draft** in `owner@example.com`'s
   Drafts folder. Subject prefixed with `[CRITICAL]`; body includes
   severity / action / reasoning / source link / item_id footer.
7. **Verify the next tick is a no-op.** Wait 5 more min; re-query
   `routed_events` for the same `item_id` — still 2 rows. No third
   draft. No second Chat card.
8. **Verify the audit row.**
   ```bash
   bq query --use_legacy_sql=false --project_id=agency-brain-demo \
     "SELECT agent_id, success, output FROM agent_audit_log.events WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 MINUTE) ORDER BY timestamp DESC LIMIT 5"
   ```
   Expected: `agent_id='routing-fanout'`, `success=true`, output
   summary mentions `dispatched=` containing both channels.

If anything fails, see "Rollback" below.

### 6. Rollback

If the smoke fails, swap the image back to the previous tag:

```bash
# Find the previous tag — usually one of adr-0025-routed-events,
# adr-0026-dedup, or the last known-good tag.
gcloud run jobs update asb-routing-fanout \
  --image=us-central1-docker.pkg.dev/agency-brain-demo/asb-agents/routing-fanout:<previous-tag> \
  --region=us-central1 \
  --project=agency-brain-demo
```

The `serviceAccountTokenCreator` binding stays — it's a no-op when the
old image doesn't try to use it. The new env vars also stay; the old
image ignores them.

## Tear-down (if abandoning the Chat lane)

```bash
gcloud scheduler jobs pause asb-routing-fanout-5m --location=us-central1 --project=agency-brain-demo

# Then in TF:
terraform plan -destroy \
  -target=module.agent_runtime.google_cloud_scheduler_job.tb_routing_fanout_5m \
  -target=module.agent_runtime.google_cloud_run_v2_job.tb_routing_fanout \
  ...
```

Pause-before-destroy: ask for explicit approval before applying any
destroy plan, even targeted.
