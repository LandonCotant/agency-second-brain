# security — Runtime audit triggers + kill switch + Model Armor (WS-F)

## PR roadmap

WS-F ships across multiple PRs. Acceptance doc fully signs off only when all three land.

| PR | Scope | Status |
|----|-------|--------|
| **PR #1** | Hardened logic + tests for the three PR-gate scripts: `scripts/least_privilege_check.py`, `scripts/hipaa_filter_check.py`, `scripts/model_armor_check.py`. 680 lines + 40+ test cases. | Shipped |
| **PR #2** | Runtime audit scripts under `src/agency_brain/audit/`: `hipaa_isolation_check.py` (hourly), `hipaa_iam_drift.py` (daily), `drafts_boundary_check.py` (nightly), `bucket_iam_drift.py` (daily — ADR 0005 compensating control). Cloud Scheduler triggers + Cloud Run hosts in `runtime_audits.tf`. Per-script SAs with custom roles. Module wired into `envs/prod`. | Shipped |
| PR #3 | `agent_kill_switch` Secret Manager flag (read by WS-C base agent class). Operational runbooks: `secret_rotation.md`, `dwd_scope_review.md`, `security_review_checklist.md`. Security tests at `tests/security/`. | Pending |

## Why this split

- PR #1 is the highest-leverage change in the entire WS-F scope: every other workstream's PRs run through these gates, so hardening them first raises the bar for all downstream work.
- PR #2 depends on WS-B PR-2 (sync writes) actually existing before HIPAA isolation can have something to verify; sequencing helps.
- PR #3's kill switch requires WS-C's base agent class to consume it — coordinate via `agent_kill_switch` secret-name contract once WS-C lands.

## Resources

Shipped in **PR #2** (`runtime_audits.tf`):
- 4 Cloud Run Jobs (`asb-audit-{hipaa-iso,hipaa-iam,drafts-bnd,bucket-iam}`) executing `python -m agency_brain.audit.<script>`. One container image (`asb-audit`), per-job `args` override.
- 4 Cloud Scheduler triggers (hourly / daily 06:00 / nightly 03:00 / daily 04:00 UTC).
- 4 per-script service accounts + 4 custom IAM roles (PRD §4.2 least-privilege).
- 1 shared Cloud Scheduler invoker SA scoped to the four jobs.
- 1 Artifact Registry repo `asb-audit`.
- Resource-scoped bindings: BigQuery `dataEditor` on `agent_audit_log` for all four; `dataViewer` on `airtable_replica` + `agent_outputs` for the HIPAA isolation check.
- Notifications reuse the existing `asb-hipaa-guard-tripped` log-based metric in `terraform/modules/observability/alerts_hipaa.tf` — the structured stdout from the audit reporter satisfies the metric's filter without any new TF.

Lands in **PR #3**:
- Secret Manager `agent_kill_switch` flag (read by the WS-C base agent class).
- Lower-severity drift alerts (Chat / email channels for IAM / bucket / drafts boundary findings if PR #2's audit-log-row-only model proves insufficient).

Lands later (WS-G):
- Model Armor regional config defaults (each Reasoning Engine TF sets `ModelArmorConfig` directly).

See [docs/acceptance/ws-f-security.md](../../../docs/acceptance/ws-f-security.md) for the full sign-off checklist.
