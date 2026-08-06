# ADR 0008 — Chat alerting via the native `google_chat` channel type

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-E (Observability)
**Supersedes:** [ADR 0007](0007-chat-alerting-via-space-webhook.md)

## Context

PRD §8.2 calls for a Chat DM to the operator as the P0 notification path for the
HIPAA isolation breach alert. ADR 0007 implemented this with Cloud
Monitoring's `webhook_tokenauth` channel pointing at a Google Chat
incoming-webhook URL stored in Secret Manager.

During smoke test of WS-E PR #1 we discovered the integration is broken:
- Cloud Monitoring `webhook_tokenauth` posts its native incident-JSON
  schema (`{"incident": {...}, "version": "1.2"}`) to the URL.
- Google Chat incoming webhooks expect `{"text": "..."}` or Cards JSON.
- Chat returns HTTP 400 on the wrong format — message silently dropped.
- Direct `curl` with the correct format reached the space (HTTP 200), so
  the URL itself is fine; the format mismatch is irreconcilable without
  an adapter in front.

A native channel type `google_chat` is available in Cloud Monitoring (BETA
launchStage). It uses the Google Cloud Monitoring Chat app, not an
incoming-webhook URL, and Cloud Monitoring handles the payload formatting
on its side.

## Decision

Replace the `webhook_tokenauth` channel with `google_chat`.

Configuration: one label, `space = "spaces/{space_id}"`. The space ID is
passed as `var.chat_space_id` in `terraform/envs/prod/terraform.tfvars`
(non-sensitive — the space ID alone confers no posting permission; the
permission comes from the Cloud Monitoring app being a member of the
space).

The Secret Manager secret `chat-webhook-brain-alerts` and the data source
that reads it are removed from Terraform. The secret itself is deleted
out-of-band after apply succeeds.

## Operator prerequisite

The **Google Cloud Monitoring** Chat app must be added to the target space
before first apply. One-time manual step:
1. Open the Brain alerts Chat space.
2. Click the space name → **Apps & integrations** → **+ Add apps**.
3. Search **Google Cloud Monitoring** → **Add to space**.

This is documented in `docs/runbooks/observability_tuning.md`.

## Why this over alternatives

- **`google_chat` (chosen).** Native, no adapter, Cloud-Monitoring-formatted
  messages with incident metadata + a "View in Cloud Console" link out of
  the box. One operator step (install the app). BETA but stable enough
  per Google docs; downgrade risk is small for an internal tool.
- **Pub/Sub + Cloud Function adapter.** Pure-Terraform, no Chat-app step,
  but ~50 lines of Cloud Function code + IAM + an additional SA. More
  surface to maintain. Defer this option until we need richer Chat
  formatting (e.g., severity-coded card layouts) that the native channel
  doesn't support.
- **Email-only (drop Chat entirely).** Loses the PRD §8.2 "Chat DM" path
  and the immediacy that came with it.

## Consequences

- The notification channel resource is replaced (destroy + recreate). The
  alert policy's `notification_channels` reference re-binds to the new
  channel ID on the same apply.
- The orphaned `chat-webhook-brain-alerts` Secret Manager secret + 3
  versions are deleted post-apply (a leaked webhook URL token sat in the
  secret history, plus in this conversation transcript — rotating is
  worth a follow-up but the URL was test-grade).
- Future Chat alerts (sync-failure, agent-error, Model Armor spike — WS-E
  PR #2) reuse the same channel by referencing
  `module.observability.chat_brain_alerts_channel_id`.

## Revisit if

- Native `google_chat` channel exits BETA with a breaking change.
- We need richer Chat formatting (Cards) than the native channel supports.
- Multi-tenant evolution (PRD §17) requires per-tenant Chat spaces — at
  which point the channel becomes a `for_each` over tenants and the
  native channel may be limiting.
