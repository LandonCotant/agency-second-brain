# HIPAA isolation breach alert (PRD §8.2 row 1).
#
# Severity: highest. Per PRD §4.1 layer 5 + acceptance doc, ANY HIPAA isolation
# event must page the operator and halt agent execution. The kill-switch (feature
# flag in Secret Manager) is owned by WS-F; this PR wires the alert path only.
# The alert documentation tells the operator to flip the flag manually until WS-F
# automates it.
#
# Signal sources that will write HIPAA_GUARD_TRIPPED into Cloud Logging:
# - WS-C base agent class HIPAA pre-flight (PRD §6.1) — every agent invocation
# - WS-F audit/hipaa_isolation_check.py hourly cron (PRD §4.1 layer 5)
# - Manual operator action during incident response
#
# Filter is intentionally broad in PR 1 — we don't yet know which payload
# field each emitter uses, and false positives in week 1 are zero risk
# (no agents running). PR 2 narrows by resource.type once agent emissions
# are observed.

resource "google_logging_metric" "hipaa_guard_tripped" {
  project = var.brain_project_id
  name    = "asb-hipaa-guard-tripped"

  description = "Count of log entries containing HIPAA_GUARD_TRIPPED. Any nonzero value is a P0. See PRD §4.1 + docs/runbooks/observability_tuning.md."

  filter = <<-EOT
    (textPayload:"HIPAA_GUARD_TRIPPED")
    OR (jsonPayload.event:"HIPAA_GUARD_TRIPPED")
    OR (jsonPayload.message:"HIPAA_GUARD_TRIPPED")
    OR (protoPayload.metadata.event:"HIPAA_GUARD_TRIPPED")
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "HIPAA guard tripped events"
  }
}

resource "google_monitoring_alert_policy" "hipaa_isolation_breach" {
  project      = var.brain_project_id
  display_name = "HIPAA isolation breach (P0)"
  combiner     = "OR"

  documentation {
    mime_type = "text/markdown"
    subject   = "[P0] HIPAA isolation breach detected"
    content   = <<-EOT
      A log entry containing `HIPAA_GUARD_TRIPPED` was observed in the Brain
      project. Per PRD §4.1 layer 5 this is the highest-severity signal we
      track — treat as a P0.

      **Immediate actions:**
      1. **Halt agent execution.** Until WS-F wires the automatic kill-switch,
         flip the feature flag manually:
         `gcloud secrets versions add agent-kill-switch --data-file=<(echo halted)`
         (Secret name confirmed in WS-F when it lands; check the runbook.)
      2. **Identify the source.** Query Cloud Logging:
         `gcloud logging read 'textPayload:"HIPAA_GUARD_TRIPPED" OR jsonPayload.event:"HIPAA_GUARD_TRIPPED"' --limit 20 --project ${var.brain_project_id}`
      3. **Verify scope.** Run WS-F's `audit/hipaa_isolation_check.py` to
         confirm whether HIPAA-flagged client data has actually leaked into
         any Brain table.
      4. **Document.** Open an incident note before resuming.

      Tuning + silencing: see `docs/runbooks/observability_tuning.md`.
      ADR for alert routing: `docs/adr/0007-chat-alerting-via-space-webhook.md`.
    EOT
  }

  conditions {
    display_name = "HIPAA_GUARD_TRIPPED count >= 1 over 5 minutes"

    condition_threshold {
      filter          = "resource.type=\"global\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.hipaa_guard_tripped.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  # PRD §8.2: Chat DM to the operator. Email is intentionally omitted on this alert
  # to keep it distinct from the lower-severity email-only alerts shipping in
  # PR 2 — when this fires, the Chat ping is the signal.
  notification_channels = [
    google_monitoring_notification_channel.chat_brain_alerts.id,
  ]

  alert_strategy {
    auto_close = "604800s" # 7 days
  }

  user_labels = {
    workstream = "observability"
    severity   = "p0"
    prd_ref    = "section-8-2"
  }
}
