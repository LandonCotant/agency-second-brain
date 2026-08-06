# ADR 0024 — Cost guardrails (AR cleanup, BQ partition TTL, budget alert)

**Status:** Accepted
**Date:** 2026-05-01
**Workstream:** Tier 3 housekeeping

## Context

The Brain stack runs ~$5–20/month today and has no retention policies on
its growth-prone resources:

- Three Artifact Registry repos (`asb-sync`, `asb-audit`, `asb-agents`)
  accumulate every image layer forever. Cloud Build's PR-checks rebuild
  each Dockerfile on every PR for verification; while that's
  push-on-merge-only today, every out-of-band push leaves an old
  `:bootstrap` image manifest behind.
- Three append-only BigQuery tables (`agent_audit_log.events`,
  `agent_outputs.triaged_items`, `agent_outputs.risk_flags`) are
  partitioned by DAY but have no `expiration_ms`. PRD §4.6 mandates
  retention of audit logs but doesn't require *forever*.
- There is no GCP billing budget alert. A runaway loop (Reasoning
  Engine retry storm, audit-job tight loop, bridge stuck) could quietly
  push spend off-trend without paging anyone.

Today's bill is small. The risk this ADR addresses is the bill becoming
a surprise as the build matures — fixing it now is one targeted apply;
fixing it after a year of accumulated rows is annoying.

## Decision

Three guardrails, one PR + one targeted apply + one out-of-band gcloud
command:

### 1. Artifact Registry cleanup policies on all three repos

Add identical `cleanup_policies` blocks to `asb-sync`, `asb-audit`, and
`asb-agents`:

```hcl
cleanup_policies {
  id     = "keep-recent-5"
  action = "KEEP"
  most_recent_versions { keep_count = 5 }
}
cleanup_policies {
  id     = "delete-old"
  action = "DELETE"
  condition { older_than = "7776000s" } # 90 days
}
```

The two policies are evaluated together: keep the 5 most recent
versions per tag, *and* delete anything older than 90 days. The
production `:bootstrap` tag is always one of the 5 most recent, so the
live image is never deleted by either rule.

### 2. BigQuery partition expiration on the three append-only tables

Add `expiration_ms` to the existing `time_partitioning` blocks:

| Table | Window | `expiration_ms` |
|---|---|---|
| `agent_audit_log.events` | 365 days | `31536000000` |
| `agent_outputs.triaged_items` | 730 days | `63072000000` |
| `agent_outputs.risk_flags` | 730 days | `63072000000` |

PRD §4.6 says the audit log must be queryable; one year covers the
audit window without compounding storage. The triage/risk tables hold
agent decisions worth two years for trend analysis but not forever.

`deletion_protection = true` on these tables blocks `terraform
destroy` but does **not** block partition expiration — partition
expiration runs at the BQ engine level, separate from TF state.

### 3. $50/month billing budget alert wired to Brain alerts Chat

Out-of-TF, single gcloud command (see
`docs/runbooks/cost_guardrails_setup.md`). Thresholds at 50%, 90%,
100% route to the existing native `google_chat` notification channel
that already powers HIPAA isolation alerts (ADR 0008). Non-blocking;
caps blast radius of any future runaway.

## What's not covered

- **`agent_outputs.goals` / `goal_scores`** — small Airtable-canonical
  replicas, monthly partitioned, useful for long-term trend lines. Not
  worth a TTL.
- **`airtable_replica.*`** — 10 tables hydrated from Airtable on a
  15-min `WRITE_TRUNCATE` cycle (ADR 0010). Each cycle replaces the
  whole table; storage doesn't accumulate. No TTL needed.
- **Pub/Sub retention** — already at the 7d default; the stash
  considered shaving to 3d, math doesn't justify the DLQ-recovery
  risk for a 2-person tool.
- **Single-region BQ migration** — would save ~25% on storage but
  breaks ADR 0013. Net cost over 1 year: ~$2–3 saved, hours of churn.
  Skip.
- **Audit job consolidation** (4 jobs → 1) — saves ~$0.50/mo, breaks
  the per-PRD-§4.1-layer audit architecture (ADR 0012). Not worth it.
- **Triage model switch** (`gemini-2.5-flash` → `flash-lite`) —
  deferred until `triaged_items` has ~50 real rows for a side-by-side
  eval. Captured as a follow-up, not part of this ADR.

## Consequences

- Three TF resources gain `cleanup_policies`. Three TF resources gain
  `expiration_ms`. No new resources, no destroys, no new IAM. Targeted
  plan: 0 add, 6 change, 0 destroy.
- The budget alert is operator-managed: no Cloud Build SA
  billing-account permission grant, no TF state for it. Documented in
  the runbook so it survives session turnover.
- Year-2 cost avoidance estimated at ~$15/month — not the dollar value
  but the avoided "the bill quadrupled" surprise. Year-1 avoidance
  ~$6/month.
- Reversibility: AR cleanup policies can be removed; once a deletion
  fires it's permanent. BQ partition expiration is permanent for
  partitions past the window; current data is all <30 days old, so
  apply drops nothing.

## References

- PRD §4.6 (audit log retention requirements)
- PRD §4.8 (PR-level security gates — none of which break on this kind
  of change)
- ADR 0010 (airtable sync WRITE_TRUNCATE — why `airtable_replica.*`
  doesn't need TTL)
- ADR 0012 (runtime audit architecture — why we don't consolidate the
  4 audit jobs)
- ADR 0013 (BQ datasets at US multi-region — why we don't migrate)
- ADR 0017 (Model Armor disabled — adjacent housekeeping that's
  scheduled for the 2026-05-14 audit, not this ADR)
- Original stash plan:
  `~/.claude/plans/analyze-this-project-and-snug-wilkinson.md` (this
  ADR is the productionization of that plan, with `asb-agents` added
  and the ADR number bumped from 0014 to 0024)
