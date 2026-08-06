# WS-F: Security & Compliance — Acceptance Criteria

Sign-off gate for merging the `ws-f-security` branch. Per [PRD.md](../../PRD.md) §4 (the security charter), §5.4, §7.4.

**Depends on:** WS-A (merged).
**Wall-clock target:** continuous (kickoff in week 1, hardens through week 12).

## PR roadmap

WS-F ships across multiple PRs. See [terraform/modules/security/README.md](../../terraform/modules/security/README.md) for full breakdown. Per-box PR pointers below.

## Functional checks — PR gate scripts (real logic, replacing WS-A stubs)

- [x] **`scripts/least_privilege_check.py`** — full logic across all PRs; allowlist for human user/group `roles/owner`; reject any SA holding `roles/owner`, `roles/editor`, or `*.admin` not in a documented exception. *(PR #1)*
- [x] **`scripts/hipaa_filter_check.py`** — full logic against any SQL files added by WS-B; canonical exclusion clause enforced. *(PR #1)*
- [x] **`scripts/model_armor_check.py`** — full logic against Reasoning Engine TF added by WS-G; verifies `ModelArmorConfig` on Triage / Knowledge Surfacer / Risk Watcher. *(PR #1)*

## Functional checks — runtime audit scripts *(PR #2)*

- [x] **`audit/hipaa_isolation_check.py`** runs hourly via Cloud Scheduler (PRD §4.1 layer 5). Joins every Brain BigQuery table against the canonical HIPAA flag; emits `HIPAA_GUARD_TRIPPED` on any match (trips the existing P0 alert from `alerts_hipaa.tf`). The kill-switch *flip* is deferred to PR #3; until then the operator flips it manually per the alert documentation. Airtable-side cross-check is a quarterly manual task documented in `docs/runbooks/runtime_audit_response.md`.
- [x] **`audit/hipaa_iam_drift.py`** runs daily; compares brain-project IAM against `terraform/modules/security/expected/brain_iam_baseline.json`; emits `SECURITY_DRIFT` on any add/remove. Cross-project HIPAA assertion (Brain SAs absent from `agency-hipaa-*`) is a quarterly manual `gcloud asset search-all-iam-policies` task — documented gap per ADR 0012.
- [x] **`audit/drafts_boundary_check.py`** runs nightly; verifies no service-account principal holds `roles/owner`, `roles/editor`, or any `*.admin` role. Reuses `is_forbidden_role` / `is_service_account` from `scripts/least_privilege_check.py` for parity with the PR-time gate. Workspace OAuth scope check (`gmail.send` / `gmail.modify`) deferred until DWD is provisioned (post-WS-G1).
- [x] **`audit/bucket_iam_drift.py`** (per ADR 0005 compensating control) runs daily; lists every GCS bucket in the brain project, compares IAM to `terraform/modules/security/expected/bucket_iam_baseline.json`, emits `SECURITY_DRIFT` on any add/remove.

## Functional checks — kill switch + operational runbooks *(PR #3)*

- [ ] **`agent_kill_switch`** Secret Manager flag created. Schema documented so WS-C base agent class can read it as a HIPAA-breach kill switch.
- [ ] **`docs/runbooks/secret_rotation.md`** for Airtable PAT and any LLM API keys (90-day cadence per PRD §4.5).
- [ ] **DWD scope review process** documented at `docs/runbooks/dwd_scope_review.md`. Quarterly review cadence.
- [ ] **`docs/runbooks/security_review_checklist.md`** for the week-12 internal review against PRD §4.

## Security tests (`tests/security/`) *(PR #3)*

- [ ] HIPAA isolation: assert flipping `HIPAA = true` removes the client from all Brain tables within one sync cycle.
- [ ] Drafts boundary: assert no SA has any send/modify scope.
- [ ] Model Armor: assert Triage rejects a curated set of known prompt-injection patterns.

## Documentation checks

- [x] `src/agency_brain/audit/README.md` lists every audit script + cadence + alert channel. *(PR #1 — README scaffold; implementations land in PR #2)*
- [ ] Each new ADR for security deviations. *(as needed across PRs)*
- [ ] `docs/dwd_scopes.md` is the canonical scope registry — update on every grant. *(populated by agent workstreams over time)*

## Sign-off

- [ ] the implementer — _____________________________ — date: __________
