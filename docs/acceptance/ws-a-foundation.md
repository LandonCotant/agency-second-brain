# WS-A: Foundation — Acceptance Criteria

Sign-off gate for merging the `ws-a-foundation` branch. Each box must be checked by the operator before WS-A is considered done. Per [PRD.md](../../PRD.md) §7.4.

## Functional checks

- [ ] **Repo bootstrap.** Fresh `git clone` of the GitHub repo + `make install` + `pre-commit run --all-files` exits 0.
- [ ] **Terraform plan/apply.** `cd terraform/envs/prod && terraform init && terraform plan` shows the expected resources; `terraform apply` succeeds end-to-end (after `bootstrap_tfstate.sh`).
- [ ] **Project existence.** `gcloud projects describe agency-brain-demo` succeeds; shows org parent `example.com` (org ID `000000000000`). (Per ADR 0005, the originally planned `asb-audit-logs-prod` was collapsed into the Brain project due to billing-account quota; revisit when quota is raised.)
- [ ] **Project policy active (allowed member domains).** `gcloud org-policies describe iam.allowedPolicyMemberDomains --project=agency-brain-demo` returns the customer-ID restriction (`CC00000000`). Per ADR 0004, scope is project-level not org-level.
- [ ] **Project policy active (disable SA keys).** Same command for `iam.disableServiceAccountKeyCreation` returns `enforce = TRUE`.
- [ ] **Default Cloud Audit Logs visible.** Per ADR 0005 (cost), no custom audit bucket. `gcloud logging read 'logName=~"cloudaudit.googleapis.com"' --project=agency-brain-demo --limit=5` returns recent Admin Activity entries. 400-day default retention is the audit posture for v1.
- [ ] **No unowned workstream SAs.** `gcloud iam service-accounts list --project=agency-brain-demo` returns only allowlisted workstream SAs plus the GCP-auto-created Compute Engine default SA (`<projnum>-compute@developer.gserviceaccount.com`), and that compute SA shows `DISABLED: True`. See ADR 0018 for why the compute default SA exists despite `compute.googleapis.com` not being in our explicit API allowlist.
- [ ] **CI SA allowlist check.** `gcloud iam service-accounts list --project=agency-brain-demo --format=json | python3 scripts/sa_allowlist_check.py` exits 0. Test the failure path by enabling the compute default SA briefly (`gcloud iam service-accounts enable ...`) — script must exit 1 — then re-disable.
- [ ] **State backend.** `gcloud storage ls gs://asb-tfstate-prod/envs/prod/` shows `default.tfstate`; bucket has versioning on.

## CI checks

- [ ] **CI green on trivial PR.** Open a README typo PR; Cloud Build runs all steps and posts green; merging is blocked until green (verify branch protection).
- [ ] **detect-secrets blocks bad PR.** Open a PR adding a fake AWS-style key (e.g. `AKIA...`); the `detect-secrets` step fails the build.
- [ ] **least-privilege blocks bad PR.** Open a PR adding a `roles/editor` grant to a fake service-account principal in TF; the `least_privilege_check.py` step fails the build.
- [ ] **Pre-commit blocks bad commit locally.** `echo "AKIA1234567890ABCDEF" > test.txt && git add test.txt && git commit -m test` is rejected by detect-secrets pre-commit.

## Documentation checks

- [ ] PRD copy at `PRD.md` matches the inbox source.
- [ ] Spec + goal hierarchy copies at `docs/source/`.
- [ ] ADR 0001, 0002, 0003 written.
- [ ] `docs/dwd_scopes.md` template present with empty table.
- [ ] `docs/runbooks/cloud_build_setup.md` written (records the manual GitHub-App connection step).
- [ ] CODEOWNERS, .gitignore, .gitattributes, .editorconfig present.
- [ ] README links to PRD + spec + workstream status table.

## Open items logged (not resolved)

- [ ] **Vantage cross-project topology** — issue/TODO captured for WS-B kickoff (per PRD Appendix B). NOT resolved by WS-A.
- [ ] **`brain-agent@example.com` Workspace user** — not created in WS-A; agents handle their own DWD subject when they ship.

## Sign-off

- [ ] the implementer — _____________________________ — date: __________
