# Runbook: Connect GitHub repo to Cloud Build

One-time manual step. Cloud Build's GitHub App connection cannot be created via Terraform without an org-level installation that we don't otherwise need.

## Prerequisite: custom Cloud Build SA must exist

The legacy `<project-number>@cloudbuild.gserviceaccount.com` default SA is **not** auto-created in this project — Google disabled that default for projects created after April 2024. We provision a custom SA (`asb-cloud-build-sa@agency-brain-demo.iam.gserviceaccount.com`) in `terraform/modules/foundation/iam.tf` instead. Confirm it exists before proceeding:

```bash
gcloud iam service-accounts list --project=agency-brain-demo \
  --filter="email:asb-cloud-build-sa@*"
# Should return one row. If empty, run `terraform apply` from terraform/envs/prod first.
```

## Steps

1. Visit https://console.cloud.google.com/cloud-build/triggers?project=agency-brain-demo
2. Click **Connect Repository** → choose **GitHub (Cloud Build GitHub App)**.
3. Authenticate as `agency-lgtm` and authorize the Cloud Build GitHub App on `agency-lgtm/agency-brain`.
4. Click **Create Trigger**:
   - Name: `asb-pr-checks`
   - Event: **Pull request**
   - Source: GitHub repo `agency-lgtm/agency-brain`
   - Base branch: `^main$`
   - Configuration: **Cloud Build configuration file** at `cloudbuild.yaml`
   - Service account: `asb-cloud-build-sa@agency-brain-demo.iam.gserviceaccount.com` (custom SA; permissions managed by Terraform in `terraform/modules/foundation/iam.tf`)
5. Save.

## Branch protection

After the first successful build appears as a check on a PR, enable branch protection on `main`:

```bash
gh api -X PUT /repos/agency-lgtm/agency-brain/branches/main/protection \
  -F required_status_checks.strict=true \
  -F required_status_checks.contexts[]="asb-pr-checks (agency-brain-demo)" \
  -F enforce_admins=false \
  -F required_pull_request_reviews.required_approving_review_count=0 \
  -F restrictions=
```

Alternatively, configure via GitHub UI: Settings → Branches → Add branch protection rule for `main`. Require status check `asb-pr-checks` to pass.

## Verify

Open a trivial PR (README typo). The check should appear and pass within ~3 minutes.

The PR-event trigger only fires on new pull-request events (opened, synchronized, reopened); creating the trigger does not retroactively run it on already-open PRs. To fire it on an existing PR, push a fresh commit to that branch.
