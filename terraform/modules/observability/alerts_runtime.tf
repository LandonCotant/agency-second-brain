# Runtime alert: Cloud Run job execution failures.
#
# Surfaced by the 2026-05-13 incident where the Calendar Ingester
# (asb-calendar-ingester) silently failed every daily 06:30 PT
# scheduler tick from 2026-05-09 → 2026-05-13. The Python writer hit
# `Required field revision_id cannot be null` on every event,
# returned 360 errors, the Job exit-coded 1, and the scheduler logged
# "execution failed" — but no alert routed because the existing
# observability surface only catches HIPAA breaches (alerts_hipaa.tf)
# and cost events (alerts_cost.tf). Four days of silent corpus loss.
#
# This alert fills the gap: ANY Cloud Run job execution that produces
# an ERROR-severity log line pages the Brain alerts Chat space + email.
# Treats every job (Triage bridge, Routing fan-out, Risk Watcher,
# Notes Ingestor, Librarian, Morning Brief, Evening Reflection, CRM
# Auto-updater, Calendar Ingester, Brag Spotter, Captures
# Materializer, audit jobs) uniformly. No per-job allowlist — if it
# errors, you find out.
#
# Trade-offs considered:
#   - Filter on `severity>=ERROR` (chosen): catches Python logger.error
#     calls AND container exit-non-zero. May fire on recoverable
#     mid-execution errors if any job logs them at ERROR level (none
#     of the current 14 jobs do — they log INFO for normal operation).
#   - Alternative: filter on Cloud Run system-event execution-failed
#     logs. More precise but the log schema is unstable and varies by
#     Cloud Run version. Skipped for now.
#   - 5-minute alignment + count>=1 threshold: matches the cadence of
#     existing alerts and minimizes wake-up noise from transient
#     hiccups while still catching the next 4-day-silent-failure
#     class. Auto-close at 7d (same as 0028/0030 alerts).

resource "google_logging_metric" "cloud_run_job_error" {
  project = var.brain_project_id
  name    = "asb-cloud-run-job-error"

  description = "Count of ERROR-severity logs from Cloud Run jobs. Surfaces silent execution failures — see the 2026-05-13 Calendar Ingester incident. Excludes asb-audit-sensitive-* jobs (HIPAA enforcement deferred per project memory `project_hipaa_deferred.md` — these jobs fail by design until HIPAA ingestion lands, ~100 expected errors/month)."

  filter = <<-EOT
    resource.type="cloud_run_job"
    AND severity>=ERROR
    AND NOT (resource.labels.job_name=~"^asb-audit-sensitive-.*")
  EOT

  label_extractors = {
    "job_name" = "EXTRACT(resource.labels.job_name)"
  }

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Cloud Run job error log events"

    labels {
      key         = "job_name"
      value_type  = "STRING"
      description = "Cloud Run job name (e.g. asb-calendar-ingester)."
    }
  }
}

resource "google_monitoring_alert_policy" "cloud_run_job_error" {
  project      = var.brain_project_id
  display_name = "Cloud Run job execution error"
  combiner     = "OR"

  documentation {
    mime_type = "text/markdown"
    subject   = "[Runtime] Cloud Run job error"
    content   = <<-EOT
      A Cloud Run job emitted an ERROR-severity log line. Investigate
      promptly — silent execution failures cost the Brain four days
      of calendar ingestion on 2026-05-09 → 2026-05-13.

      **Identify which job + recent failure cause:**
      ```bash
      gcloud logging read \
        'resource.type="cloud_run_job" AND severity>=ERROR' \
        --project=${var.brain_project_id} \
        --freshness=15m --limit=10 \
        --format='value(timestamp,resource.labels.job_name,textPayload)'
      ```

      **List recent failed executions for a specific job:**
      ```bash
      gcloud run jobs executions list \
        --job=<job-name> --region=${var.region} \
        --project=${var.brain_project_id} \
        --limit=5 --format='value(metadata.name,status.conditions[0].status)'
      ```

      **Common failure classes:**
      - Schema/required-field mismatches on `agent_outputs.notes` MERGE
        (ADR 0037 §3). Surfaced today as the Calendar Ingester
        `revision_id` bug. Fix at the writer + redeploy.
      - DWD token-creator IAM gaps (cf. PR #72 self-impersonation
        fix, PR #117 runWithOverrides binding).
      - Missing Python deps after a new import — check the agent's
        Dockerfile pin set against `from X import Y` lines per the
        codebase gotcha in CLAUDE.md.
      - Airtable PAT rotation lag (`airtable-pat-prod` /
        `airtable-tasks-write-pat-prod` in Secret Manager).

      Context: 2026-05-13 Calendar Ingester silent-failure incident.
      This alert was added to prevent the next 4-day-silent regression.
    EOT
  }

  conditions {
    display_name = "Cloud Run job error count >= 1 over 5 minutes"

    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.cloud_run_job_error.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
        # Sum across all jobs so one alert fires regardless of which
        # job errored. The Chat card includes the metric label so the
        # operator sees the job_name in the incident detail.
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
    auto_close = "604800s" # 7 days, matches 0028/0030 alerts
  }

  user_labels = {
    workstream = "observability"
    severity   = "high"
    purpose    = "runtime-failure-alert"
    incident   = "2026-05-13-calendar-silent"
  }
}
