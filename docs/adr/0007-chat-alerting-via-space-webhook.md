# ADR 0007 — Chat alerting via incoming webhook in a single-human space

**Status:** SUPERSEDED by [ADR 0008](0008-chat-alerting-via-google-chat-channel.md)
**Date:** 2026-04-25
**Superseded:** 2026-04-25 (same day — discovered during smoke test)
**Workstream:** WS-E (Observability)

## Why superseded

Cloud Monitoring's `webhook_tokenauth` channel sends a fixed incident-JSON
payload that Google Chat's incoming webhooks 400 on. Direct `curl` to the
webhook with `{"text": "..."}` works, but Cloud Monitoring's payload doesn't
match — alerts silently drop. We discovered this when the smoke-test alert
fired (verified at the metric + incident layer) but no message landed in
Chat. ADR 0008 switches to the native `google_chat` channel type via the
Google Cloud Monitoring Chat app, which Cloud Monitoring formats correctly.

The original content below is preserved for context.

---

## Context

PRD §8.2 specifies "Chat DM to the operator" (now the operator) as the notification path
for the HIPAA isolation breach alert (P0). Cloud Monitoring's notification
channels offer two ways to reach Google Chat:

1. **`google_chat` channel type.** Uses the Cloud Monitoring Chat app (a
   Google-published bot) installed in a Chat space. Authentication is
   IAM-based; no secrets to manage. Required label: the space name (e.g.
   `spaces/AAAxxxxx`), which is not sensitive.
2. **`webhook_tokenauth` channel type.** Uses a Chat space's incoming
   webhook URL (which contains an auth token). The full URL is a
   credential.

Neither channel type can DM a single user — Cloud Monitoring → Chat is always
space-scoped. PRD §8.2's "DM to the operator" therefore has to be operationalized
as a space whose only human member is the operator.

## Decision

**Use `webhook_tokenauth` against an incoming webhook in a "Brain alerts"
Chat space, with the webhook URL stored in Secret Manager.**

The webhook URL goes in a Secret Manager secret named
`chat-webhook-brain-alerts`. Terraform reads it via
`data "google_secret_manager_secret_version"` on every plan; the resulting
`secret_data` is automatically marked sensitive by the provider, so the URL
never appears in plan output or non-encrypted state diffs.

The "Brain alerts" Chat space contains one human (the operator) plus the webhook
bot. PR 2's lower-severity Chat alerts (sync failure, agent error rate)
reuse the same channel.

## Rationale

Why webhook over `google_chat` channel type:

- **Setup is simpler and self-contained.** Creating an incoming webhook is
  two clicks inside the Chat space and produces an immediately usable URL.
  Installing the Cloud Monitoring Chat app requires Workspace admin to
  approve the app in the marketplace, which adds a second person to the
  setup loop and an opaque approval step. For a 2-person tool we prefer
  the path that one person can complete end-to-end.
- **No org-wide app surface.** The Cloud Monitoring app, once installed,
  can be added to any space in the Workspace by any user. The webhook is
  scoped to one space.
- **Predictable rotation story.** Webhook URL rotation is a documented
  Secret Manager flow that fits the PRD §4.5 90-day rotation cadence.
  The Chat-app path has no rotation concept (it's IAM-based) which is
  cleaner in some ways but offers no recourse if the app is compromised
  short of uninstalling.

Why not direct DM:
- Cloud Monitoring doesn't support per-user DM as a channel type. A
  single-human space is the only available shape.

Why secret over plain tfvars for the URL:
- The URL contains an auth token. PRD §4.5 forbids secrets in tfvars,
  `.env` files, or code. Secret Manager + a sensitive-marked data source
  is the standard pattern in this repo.

## Consequences

- One-time manual setup before first apply: create the Chat space, add the
  webhook, populate the secret. Documented in
  `docs/runbooks/observability_tuning.md`.
- Webhook URL rotation requires a new secret version + `terraform apply`.
  The notification channel resource will be replaced (Cloud Monitoring
  resource IDs are stable across in-place updates of `labels.url`, but
  testing this is on the rotation runbook to verify).
- The "Brain alerts" Chat space is single-purpose; future product Chat
  notifications (e.g. agent draft notifications, if ever added) must use a
  separate space and separate channel.

## Revisit if

- Workspace admin enables the Cloud Monitoring Chat app org-wide; the
  setup-friction argument disappears and we may prefer the IAM-based path.
- A second human joins the on-call rotation; we'd want either a shared
  space (which works with this design) or per-user DMs (which would
  require a different mechanism, e.g. PagerDuty fanning out, or a custom
  Cloud Function notification webhook).
- The webhook URL leaks; rotate immediately and consider migrating to the
  `google_chat` channel type as part of the post-incident review.
