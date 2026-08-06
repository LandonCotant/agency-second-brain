# audit — Runtime security checks (WS-F)

Periodic audit scripts that verify the security charter (PRD §4) holds at runtime, not just at PR time.

| Script | Cadence | Trigger | What it verifies |
|---|---|---|---|
| `hipaa_isolation_check.py` | hourly | Cloud Scheduler | No HIPAA-flagged record appears in any Brain table |
| `hipaa_iam_drift.py` | daily | Cloud Scheduler | Brain SAs have no IAM grants in HIPAA project |
| `drafts_boundary_check.py` | nightly | Cloud Scheduler | No agent SA has acquired a send/modify scope |
| `bucket_iam_drift.py` | daily | Cloud Scheduler | No unexpected principal on audit-bucket IAM |

Each script:
- Reads expected state from a versioned policy file in `audit/expected/`.
- Compares to live state via gcloud / BigQuery / Logging APIs.
- On drift: writes a row to `agent_audit_log.events` with `event_id = SECURITY_DRIFT` and triggers Chat DM to the operator.
- HIPAA isolation breach additionally **halts all agent execution** via a Secret Manager feature flag — read by the base agent class as a kill switch.

Per [PRD.md](../../../../PRD.md) §4.1 layer 5, §4.7, §4.8.
