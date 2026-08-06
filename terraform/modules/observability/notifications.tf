# Notification channels for Cloud Monitoring alerts.
#
# Two channels, both project-scoped to the Brain project:
# - Email to var.owner_email (verified out-of-band — Cloud Monitoring sends a
#   confirmation email on first apply; click the link to enable delivery).
# - Native google_chat channel posting to a "Brain alerts" Chat space via the
#   Google Cloud Monitoring Chat app. Cloud Monitoring formats the payload
#   correctly for Chat — webhook_tokenauth + an incoming webhook URL does NOT
#   work because Chat 400s on Cloud Monitoring's native incident schema.
#   See ADR 0008 for the design and ADR 0007 (superseded) for what we tried first.
#
# Operator prerequisite: the Google Cloud Monitoring app must be added to the
# Chat space before first apply. See docs/runbooks/observability_tuning.md.
#
# PR 2's lower-severity Chat alerts (sync failure, agent error rate) reuse
# the same Chat channel; splitting by severity is a pure-Terraform change if
# noise becomes an issue.

resource "google_monitoring_notification_channel" "email_owner" {
  project      = var.brain_project_id
  display_name = "Brain owner email (${var.owner_email})"
  type         = "email"

  labels = {
    email_address = var.owner_email
  }

  user_labels = {
    workstream = "observability"
    purpose    = "owner-email"
  }
}

resource "google_monitoring_notification_channel" "chat_brain_alerts" {
  project      = var.brain_project_id
  display_name = "Brain alerts Chat space"
  type         = "google_chat"

  labels = {
    space = "spaces/${var.chat_space_id}"
  }

  user_labels = {
    workstream = "observability"
    purpose    = "high-severity-chat"
  }
}
