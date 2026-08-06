# Audit logging — relies on Cloud Audit Logs defaults.
#
# Deviation from PRD §4.6 layer 1: no custom retention-locked GCS bucket.
# Cloud Audit Logs gives us Admin Activity, System Event, and Policy Denied
# logs free, with 400-day default retention. For a 2-person internal tool
# that's adequate; the custom sink + bucket added cost (storage + write ops)
# without meaningful incremental risk reduction. See ADR 0005.
#
# PRD §4.6 layer 2 — the application audit log at BigQuery
# `agent_audit_log.events` — is unaffected and still ships in WS-E.
#
# Promote to a custom retention-locked sink if/when:
# - Compliance regime requires > 400 days retention
# - Brain becomes multi-tenant (per spec §17)
# - A specific incident demonstrates the default retention is insufficient
