# ADR 0005 — Rely on default Cloud Audit Logs (drop custom retention sink from §4.6 layer 1)

**Status:** Accepted
**Date:** 2026-04-25
**Workstream:** WS-A (Foundation)
**Supersedes:** ADR 0002 (Provision audit-log project in WS-A)

## Context

PRD §4.6 layer 1 specified a separate write-once GCP project hosting a retention-locked GCS bucket for Cloud Audit Logs, with 18-month retention.

Two real-world signals during WS-A apply:

1. The billing account hit its project-link quota — the audit project couldn't be linked to billing without a quota increase.
2. The Brain is a 2-person internal tool, not a compliance-regulated multi-tenant product. The custom sink + bucket adds storage + log-routing cost without meaningfully changing the threat model: GCP's default Cloud Audit Logs retain Admin Activity, System Event, and Policy Denied logs for 400 days for free.

User chose to drop the custom audit infrastructure and rely on defaults.

## Decision

WS-A does **not** create:

- A separate audit-log GCP project
- A custom GCS audit bucket with retention lock
- Cloud Audit Logs project sinks routing to that bucket

WS-A relies on **default Cloud Audit Logs**:

- Admin Activity logs — automatic, 400-day retention, free
- System Event logs — automatic, 400-day retention, free
- Policy Denied logs — automatic, 30-day retention, free
- Data Access logs — NOT enabled (default); enable per-API only if a specific WS needs them

The PRD §4.6 layer 2 application audit log (`agent_audit_log.events` in BigQuery) is unaffected. WS-E owns it; agents emit structured rows on every invocation as originally planned.

## Rationale

- **Default retention is enough for 2 people.** 400 days > the typical incident-investigation window. By the time we need older logs, we'll either have grown enough to justify the upgrade or have moved on.
- **Cost predictability.** The custom bucket would have cost $0–$5/mo for log volume in the early build; that's small in absolute terms but adds operational overhead (retention lock review, IAM monitoring, sink writer SA management) that isn't justified at this scale.
- **Reversible.** If a real incident or compliance requirement demands longer retention, recreating the sink + bucket later is a small Terraform PR. The threat model doesn't change retroactively — historical Admin Activity logs would already be in the default Cloud Audit Logs for 400 days, so we'd lose nothing by deferring.

## Cleanup note

A bucket `gs://asb-audit-logs-prod-sink` was partially created during the
interrupted WS-A apply, before this ADR was decided. It has a **locked
548-day retention policy** which makes the bucket itself non-deletable until
~Oct 2027 (lock cannot be shortened). The bucket has been:

- Removed from Terraform state (`terraform state rm`) so TF doesn't try to destroy it
- The sink writing to it has been deleted (`gcloud logging sinks delete asb-brain-audit-sink`)

Result: the bucket sits empty and orphaned at zero monthly cost (empty buckets
incur no storage charges). It will be eligible for deletion in October 2027.
Do not use it; it is not the canonical audit destination per this ADR.

## Consequences

- Cloud Audit Logs are visible only to project Owners + Logs Viewer role on `agency-brain-demo`. No write-once tamper-evidence beyond GCP's platform guarantees.
- WS-F's planned `audit/hipaa_isolation_check.py` (continuous HIPAA audit) becomes more important — it's the runtime verification of HIPAA boundary that doesn't depend on long-term log retention.
- ADR 0002 is superseded.

## Revisit if

- Compliance regime requires > 400 days retention.
- A specific incident demonstrates default retention is insufficient.
- Brain becomes multi-tenant (PRD §17) — at which point external audit retention is non-negotiable.
- Cloud Audit Logs default behavior changes (e.g., free tier shrinks).
