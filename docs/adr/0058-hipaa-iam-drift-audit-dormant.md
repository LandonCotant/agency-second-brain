# ADR 0058 — Pause `asb-audit-sensitive-iam-drift` scheduler while HIPAA cluster is dormant

**Status:** Superseded by re-enablement — 2026-06-10. The audit was re-enabled (baseline refreshed from live policy, image rebuilt, `paused = false`) as a 2026-06-10 code-review follow-up; the refresh diff was exactly the legitimate post-WS-G1 additions this ADR anticipated (12 custom agent roles + 2 GCP-managed service agents, no member drift). The re-enable checklist below was executed. Originally Accepted 2026-05-19, extending ADR 0055 to the second HIPAA-named audit.

## Context

`asb-audit-sensitive-iam-drift` runs daily at 06:00 UTC. Despite the `hipaa_` prefix in its name and SA, the audit is **not** HIPAA-data-scoped — it reads the brain project's IAM policy via `cloudresourcemanager.projects.getIamPolicy` and diffs it against `terraform/modules/security/expected/brain_iam_baseline.json` (PRD §4.1 layer 1, "no Brain SA bound to a high-privilege role out-of-band"). Source: `src/agency_brain/audit/hipaa_iam_drift.py:1-26`.

Every execution since at least 2026-05-15 has exited with code 2 (= `SECURITY_DRIFT` detected). The 2026-04-29 run captured the actual diff:

- Added (vs. baseline, all legitimate):
  - `roles/aiplatform.serviceAgent`, `roles/aiplatform.reasoningEngineServiceAgent` — GCP-managed service agents created when Reasoning Engine APIs were first enabled.
  - `roles/modelarmor.serviceAgent` — same, created when Model Armor was enabled (now retired per ADR 0017 but the agent binding lingers).
  - `roles/containeranalysis.ServiceAgent` — same, created by Artifact Registry container scanning.
  - `roles/viewer` + `roles/logging.logWriter` on `asb-cloud-build-sa@agency-brain-demo` — intentional Cloud Build SA migration.
  - Custom `projects/.../roles/tbAgentTriage` on `asb-agent-triage-sa` — ADR 0027.
- Removed: `roles/viewer` on the default `000000000000@cloudbuild.gserviceaccount.com` — paired with the migration above.

The baseline file (`brain_iam_baseline.json`) was last refreshed at commit `a42bb24` post-WS-G1 in late April. Since then we've shipped ADRs 0028 (RE service agents implied), 0044 (Drive write SA), 0047 (CRM auto-updater), 0048 (Solutions Drive ingestion SA), 0049 (Gmail-into-corpus SA), 0050 (Brain API caller SA), 0054 §2, 0056 (scheduler retirements), 0057 (personal CRM bridge SA) — each touching IAM intentionally — and GCP has lazily created service agents along the way. The baseline is structurally stale; the audit is correctly screaming about it.

The 2026-05-13 `Cloud Run job execution error` alert policy (added post-calendar-silent incident, fires on ERROR ≥ 1 over 5 min across all Cloud Run Jobs) routes the exit-code-2 → ERROR system-event chain into the user's inbox + Chat every day at 06:00 UTC.

## Decision

Pause `asb-audit-sensitive-iam-drift-cron` via Terraform, matching the ADR 0055 surgical pattern. Flip `paused = false` → `paused = true` in `local.audit_jobs.hipaa_iam_drift` in `terraform/modules/security/runtime_audits.tf`. Job, SA, custom role, audit-log destination, and image stay deployed. One-line re-enable.

## Why this is correct given the diagnosis differs from ADR 0055

ADR 0055's audit (`hipaa_isolation_check`) was failing because it queried a HIPAA-data table that has never existed; pausing was correct because HIPAA ingestion is deferred and the audit would assert over an empty set even if fixed. This audit is the opposite — it's functioning correctly, detecting legitimate drift the operator chose not to address yet.

The reason pausing is still the right move here:

1. **User intent is to quiet the HIPAA-named cluster while it's dormant.** This audit is the second-loudest HIPAA-named job; the alert noise is the immediate pain. ADR 0055 already paused its sibling.
2. **The audit's signal is not load-bearing right now.** Out-of-band IAM grants on a 2-person prod project are caught by other means: every IAM change goes through PRs touching `terraform/`, and PR review + `least_privilege_check.py` covers the high-risk surface. The audit is a defense-in-depth check, not the primary control.
3. **The cost to keep it green is real and the marginal risk is low** (cf. user preference `feedback_security_vs_cost`): refreshing the baseline requires vetting every current binding against current Terraform + known service agents, rebuilding the `asb-audit` image, redeploying, and committing the new JSON. Worth doing when we want the signal back; not worth doing every time we ship an ADR that touches IAM unless we know the signal is needed.

## Alternatives rejected

**Refresh baseline + redeploy now.** ~30 minutes of work (capture live policy, vet each binding, commit, rebuild `cloudbuild.audit.yaml`, roll the Cloud Run Job). Rejected because it doesn't align with user intent and the audit re-enters the same staleness trap the next time any ADR-driven IAM change ships. The re-enable checklist below makes this a deliberate operator-driven refresh when the signal becomes load-bearing again.

**Suppress this job in the alert policy.** Same rejection as ADR 0055: precedent erosion. Per-job exclusions in `asb-cloud-run-job-error` turn the alert into a denylist that drifts out of sync.

**Full teardown (destroy SA, custom role, Job).** Rejected because: idle Cloud Run Job + SA + scheduler-paused cron cost ~$0/mo; teardown means rewriting the Terraform later when we want IAM-drift detection back; the audit code + bundled-baseline pattern is already validated and worth preserving.

**Refactor the audit to skip cleanly when the baseline is "obviously stale" (e.g., compare role-count delta).** Rejected — the audit's signal is "any drift = investigate"; teaching it to ignore "big" drift defeats the purpose, and the 2026-05-13 calendar-silent incident is exactly the precedent against "skip silently" patterns.

## Re-enable checklist

When IAM-drift detection becomes load-bearing again (e.g., when granting cross-project IAM or adding external collaborators):

- [ ] Capture live policy: `gcloud projects get-iam-policy agency-brain-demo --format=json` (or run the audit Job with `--capture` once IAM allows). Write to `terraform/modules/security/expected/brain_iam_baseline.json`.
- [ ] Vet each binding against current Terraform + known GCP-managed service agents. Anything unaccounted-for blocks the refresh — investigate before committing.
- [ ] Rebuild `asb-audit` image: `gcloud builds submit --config cloudbuild.audit.yaml` (baseline JSON is baked in at `/app/security/expected/...` per `Dockerfile.audit`).
- [ ] Roll the Cloud Run Job to the new image revision.
- [ ] Set `paused = false` for `hipaa_iam_drift` in `terraform/modules/security/runtime_audits.tf`.
- [ ] `terraform apply -target='module.security.google_cloud_scheduler_job.audit["hipaa_iam_drift"]'`.
- [ ] Smoke-fire: `gcloud run jobs execute asb-audit-sensitive-iam-drift --region=us-central1 --project=agency-brain-demo --wait`. Expect `SECURITY_AUDIT_OK` + zero alert emails on the next 06:00 UTC tick.
- [ ] Update the `asb-audit-sensitive-iam` row in `docs/PRODUCTION_STATE.md` (drop `PAUSED (ADR 0058)` marker; bump `Last verified:`).
- [ ] Optional: rename the SA / Job / scheduler to drop the misleading `hipaa_` prefix (it's a brain-project IAM drift check, not a HIPAA check). Deferred as scope creep.

## Related

- ADR 0012 — runtime audit architecture (same shape, this is the fourth of five jobs now in some dormant/skipped state)
- ADR 0055 — `asb-audit-sensitive-isolation` paused (sibling; different diagnosis but same alert-noise driver)
- ADR 0027 / 0029 — IAM bindings that need to appear in any refreshed baseline
- Project memory `project_hipaa_deferred` — does NOT directly apply (this audit is misnamed; it's not HIPAA-scoped), but the broader posture ("quiet HIPAA-named infrastructure while dormant") is the operational driver
