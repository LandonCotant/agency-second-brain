# Pull subscription on asb-triage-input for the WS-G1 Triage Agent.
# PRD §6.3 + spec §5.1. The topic was deployed by WS-B; this is the first
# subscription on it.

# Project number — needed for the GCP-managed Pub/Sub service-agent IAM grant
# below (DLQ publishing on delivery failure).
data "google_project" "brain" {
  project_id = var.brain_project_id
}

resource "google_pubsub_subscription" "tb_triage_input_sub" {
  project = var.brain_project_id
  name    = "asb-triage-input-sub"
  topic   = "projects/${var.brain_project_id}/topics/asb-triage-input"

  # Generous to cover Reasoning Engine cold starts (~10-20s on first invocation).
  ack_deadline_seconds = 600

  # Match the topic's 7d retention so a Reasoning Engine outage doesn't drop signals.
  message_retention_duration = "604800s"

  # ADR 0026: narrows Pub/Sub-side dedup from "best-effort" to "guaranteed
  # within ack-deadline". Combined with the writer-side input_hash dedup
  # (TriagedItemWriter.find_recent_by_hash), eliminates the duplicate-row
  # failure mode end-to-end.
  enable_exactly_once_delivery = true

  dead_letter_policy {
    dead_letter_topic     = "projects/${var.brain_project_id}/topics/asb-triage-input-dlq"
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  expiration_policy {
    ttl = "" # never expire
  }

  labels = {
    workstream = "ws-g1"
    component  = "triage-input"
  }
}

# Triage SA pulls messages.
resource "google_pubsub_subscription_iam_member" "triage_subscriber" {
  project      = var.brain_project_id
  subscription = google_pubsub_subscription.tb_triage_input_sub.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.tb_agent_triage_sa.email}"
}

# Required for dead_letter_policy to actually publish on delivery failure.
# Without this, dead-letters silently fail and bad messages can loop forever.
resource "google_pubsub_topic_iam_member" "triage_dlq_publisher_pubsub_sa" {
  project = var.brain_project_id
  topic   = "asb-triage-input-dlq"
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.brain.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Reciprocal: the Pub/Sub service agent also needs subscriber on the main
# subscription to ack messages it dead-letters.
resource "google_pubsub_subscription_iam_member" "triage_dlq_subscriber_pubsub_sa" {
  project      = var.brain_project_id
  subscription = google_pubsub_subscription.tb_triage_input_sub.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.brain.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
