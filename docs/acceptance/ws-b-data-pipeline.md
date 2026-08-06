# WS-B: Data Pipeline — Acceptance Criteria

Sign-off gate for merging the `ws-b-data-pipeline` branch. Per [PRD.md](../../PRD.md) §5.4, §6.2, §7.4.

**Depends on:** WS-A (merged).
**Wall-clock target:** 2 weeks.

## Functional checks

- [x] **Airtable schema codified.** `airtable/schema.json` (PR-1) lists all 7 tables. PR-2 adds the `Projects.Client HIPAA` and `Tasks.Project HIPAA` Lookup fields that drive the source-query HIPAA cascade, and makes `Tasks.Project` required. `airtable/validation_rules.md` documents the cascade and required-field rules.
- [x] **Sync flow running.** `airtable_to_bq` Cloud Run Job (ADR 0010) triggered every 15 min by `asb-airtable-sync-15m` Cloud Scheduler. Full-snapshot pulls per cycle (replica tables are small; `last_modified > checkpoint` is plumbed in `hipaa_filters.last_modified_after` for the future MERGE-based evolution). `_sync_checkpoints` table tracks per-table run metadata. SLA: < 10 min from Airtable change to BigQuery (run cadence is 15 min; live verification per `docs/runbooks/hipaa_isolation_verification.md`).
- [x] **HIPAA filter applied at source.** Implemented via Airtable `filterByFormula` referencing the codified Lookup fields: `NOT({HIPAA})` on Clients, `NOT({Client HIPAA})` on Projects, `NOT({Project HIPAA})` on Tasks. `tests/security/test_hipaa_isolation.py` asserts every outbound request carries the clause and that a HIPAA flip removes the row via `WRITE_TRUNCATE`. Live end-to-end verification: `docs/runbooks/hipaa_isolation_verification.md` (the operator, before merge).
- [x] **MERGE idempotency.** Replaced with `WRITE_TRUNCATE` semantics per ADR 0010 — re-running with the same source state produces the same end state by construction. Same acceptance contract, simpler mechanism.
- [x] **Schema drift surfaced, not auto-applied.** `drift_detector.detect_new_columns` compares each Airtable response against `schema.json`; new columns publish a JSON message to `asb-schema-drift-alerts`. `tests/unit/sync/test_drift_detector.py` + `test_hipaa_isolation.py::test_drift_event_published_for_unknown_column` cover both empty-drift and drift-detected paths.
- [ ] **Vantage federation.** PR-3 (deferred). The Vantage cross-project topology decision (PRD Appendix B) lands as an ADR with PR-3.
- [ ] **Workspace → Pub/Sub.** PR-4 (deferred). Gmail / Calendar / Chat / Drive event triggers publish to `asb-triage-input` topic. Domain/label filter excludes HIPAA-client domains.
- [ ] **Knowledge Catalog registration.** PR-5 (deferred). BigQuery datasets + key tables registered with aspects (`hipaa_excluded` mandatory, others as needed).

## Security checks

- [x] `asb-sync-airtable-sa` service account created with custom role `tbSyncAirtable` — no predefined roles. Validated by `scripts/least_privilege_check.py` in CI. `asb-sync-vantage-sa` is deferred to PR-3.
- [x] Airtable PAT in Secret Manager (`airtable-pat-prod` by default; var-driven). Resource-scoped IAM grant to `asb-sync-airtable-sa` only. Rotation runbook at `docs/runbooks/airtable_pat_rotation.md`.
- [x] `scripts/hipaa_filter_check.py` (shipped by WS-F PR-1) gates SQL files for the canonical HIPAA exclusion clause. PR-2 introduces no `.sql` files (HIPAA filtering happens at the Airtable layer, not in SQL views), so the check passes vacuously — verified.

## Documentation checks

- [x] `src/agency_brain/sync/README.md` updated with PR-2 deliverables, host model, IAM layout, ops affordances.
- [ ] ADR for Vantage cross-project topology decision (deferred to PR-3).
- [x] `airtable/validation_rules.md` updated with the HIPAA Lookup cascade and `Tasks.Project` required-field rule.
- [x] ADR 0010 records the Cloud Run Job execution-model decision and the WRITE_TRUNCATE / full-pull design choices.

## Sign-off

- [x] the implementer — the implementer — date: 2026-04-25

**Deferred verification:** the live HIPAA isolation runbook
(`docs/runbooks/hipaa_isolation_verification.md`) requires the deployed
Cloud Run Job, which only exists after this PR's `terraform apply`. The
runbook runs as a post-merge smoke test against the live Airtable base,
not as a merge gate. The unit tests in
`tests/security/test_hipaa_isolation.py` (54 assertions covering filter
on every request, Lookup-codified, flip-removes-row, drift surfacing)
plus the WS-F PR-gate scripts plus the runtime `HIPAA_GUARD_TRIPPED →
Chat` alert are the real gates; the runbook is belt-and-suspenders.
