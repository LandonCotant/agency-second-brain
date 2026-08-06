# Cost guardrail: alert on Vertex AI Reasoning Engine creation events.
#
# See ADR 0028. Designed to catch the 2026-05-02 incident pattern: an
# RE was deployed via the SDK path (ADR 0016), the prior engine was not
# deleted, and four engines billed in parallel for ~3 days before the
# spend was noticed. Every CreateReasoningEngine API call now pings the
# Brain alerts Chat space + owner email so the operator can confirm
# cleanup was performed.

resource "google_logging_metric" "reasoning_engine_created" {
  project = var.brain_project_id
  name    = "asb-reasoning-engine-created"

  description = "Count of Vertex AI Reasoning Engine creation events. ADR 0028 — every creation should be paired with deletion of the prior engine."

  filter = <<-EOT
    protoPayload.serviceName="aiplatform.googleapis.com"
    AND protoPayload.methodName="google.cloud.aiplatform.v1.ReasoningEngineService.CreateReasoningEngine"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Reasoning Engine creation events"
  }
}

resource "google_monitoring_alert_policy" "reasoning_engine_created" {
  project      = var.brain_project_id
  display_name = "Reasoning Engine created (cost guardrail)"
  combiner     = "OR"

  documentation {
    mime_type = "text/markdown"
    subject   = "[Cost] New Vertex AI Reasoning Engine created"
    content   = <<-EOT
      A `CreateReasoningEngine` audit event was observed in the Brain
      project. ADR 0028: every RE deploy should be paired with
      deletion of the prior engine — orphans bill 24/7 at vCPU+memory
      hour rates.

      **Verify cleanup:**
      ```bash
      curl -s -X GET \
        "https://us-central1-aiplatform.googleapis.com/v1/projects/${var.brain_project_id}/locations/us-central1/reasoningEngines" \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); [print(e['name'].split('/')[-1], e.get('displayName'), e.get('createTime')) for e in d.get('reasoningEngines',[])]"
      ```

      Expected count is 1 (the live `asb-agent-triage` engine). If you
      see more, delete the orphans:
      ```bash
      curl -X DELETE \
        "https://us-central1-aiplatform.googleapis.com/v1/projects/${var.brain_project_id}/locations/us-central1/reasoningEngines/<ID>?force=true" \
        -H "Authorization: Bearer $(gcloud auth print-access-token)"
      ```

      Context: ADR 0016 (RE deploy path), ADR 0028 (this guardrail).
    EOT
  }

  conditions {
    display_name = "RE creation count >= 1 over 5 minutes"

    condition_threshold {
      filter          = "resource.type=\"audited_resource\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.reasoning_engine_created.name}\""
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

  notification_channels = [
    google_monitoring_notification_channel.chat_brain_alerts.id,
    google_monitoring_notification_channel.email_owner.id,
  ]

  alert_strategy {
    auto_close = "604800s" # 7 days
  }

  user_labels = {
    workstream = "observability"
    severity   = "low"
    purpose    = "cost-guardrail"
    adr_ref    = "0028"
  }
}

# ---------------------------------------------------------------------------
# Daily spend threshold breach (ADR 0030)
# ---------------------------------------------------------------------------
# Fires when asb-audit-cost-daily-check emits a structured COST_THRESHOLD_EXCEEDED
# log line — i.e. at least one monitored project exceeded its configured
# daily threshold. Routes to the same Chat + email channels as the RE-create
# alert.

resource "google_logging_metric" "cost_threshold_exceeded" {
  project = var.brain_project_id
  name    = "asb-cost-threshold-exceeded"

  description = "Count of COST_THRESHOLD_EXCEEDED events from the daily spend check. ADR 0030."

  filter = <<-EOT
    jsonPayload.event="COST_THRESHOLD_EXCEEDED"
    AND jsonPayload.agent_id="audit-cost-daily-check"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Daily spend threshold breach events"
  }
}

resource "google_monitoring_alert_policy" "cost_threshold_exceeded" {
  project      = var.brain_project_id
  display_name = "Daily spend threshold exceeded"
  combiner     = "OR"

  documentation {
    mime_type = "text/markdown"
    subject   = "[Cost] Daily spend threshold exceeded"
    content   = <<-EOT
      The daily spend check (`asb-audit-cost-daily-check`) detected that
      one or more monitored projects exceeded its configured daily spend
      threshold (ADR 0030). The Chat card posted by the same job has
      the per-project breakdown; this alert is the loud follow-up.

      **Verify which project + service is driving the spend:**
      ```bash
      gcloud logging read \
        'jsonPayload.event="COST_THRESHOLD_EXCEEDED"' \
        --project=${var.brain_project_id} --limit=1 --format=json \
        | jq '.[0].jsonPayload.summary'
      ```

      **If you decide to shut everything down** (manual kill switch):
      ```bash
      bash scripts/disable_billing.sh <PROJECT_ID>
      ```

      Per-project thresholds are configured in
      `terraform/modules/security/main.tf` (`var.cost_thresholds_usd`).
      To re-tune, edit the map and run a targeted apply on the
      `asb-audit-cost-daily-check` job — the env var picks up the new
      values on the next scheduled execution.

      Context: ADR 0030 (daily spend check), ADR 0028 (RE-create alert).
    EOT
  }

  conditions {
    display_name = "COST_THRESHOLD_EXCEEDED count >= 1 over 5 minutes"

    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.cost_threshold_exceeded.name}\""
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

  notification_channels = [
    google_monitoring_notification_channel.chat_brain_alerts.id,
    google_monitoring_notification_channel.email_owner.id,
  ]

  alert_strategy {
    auto_close = "604800s" # 7 days
  }

  user_labels = {
    workstream = "observability"
    severity   = "high"
    purpose    = "cost-guardrail"
    adr_ref    = "0030"
  }
}
