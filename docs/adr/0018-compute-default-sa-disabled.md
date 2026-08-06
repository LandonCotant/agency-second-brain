# ADR 0018 — Compute Engine default SA: disabled, not deleted

**Status:** Accepted (amended 2026-04-29)
**Date:** 2026-04-27
**Workstream:** WS-A (Foundation), follow-up cleanup

## Context

WS-A's foundation Terraform module enables a deliberate allowlist of 12 APIs on `agency-brain-demo`: `cloudresourcemanager`, `iam`, `iamcredentials`, `serviceusage`, `cloudbuild`, `secretmanager`, `logging`, `monitoring`, `storage`, `pubsub`, `cloudkms`, `orgpolicy`. `compute.googleapis.com` is **not** in that list.

After the first apply, `gcloud services list --enabled` showed seven additional APIs that we did not request — most notably `compute.googleapis.com` — and `gcloud iam service-accounts list` showed a Compute Engine default service account `<project_number>-compute@developer.gserviceaccount.com`.

PRD §4.2 ("No shared service accounts. Each agent, sync flow, and routing flow gets its own SA…") forbids unowned SAs on the project. We have one anyway.

## Diagnosis

Enabling `cloudbuild.googleapis.com` (which we do need — it powers PR CI) transitively enables:

```
cloudbuild.googleapis.com
  → containerregistry.googleapis.com
      → compute.googleapis.com           ← creates the compute default SA
      → oslogin.googleapis.com
  → containeranalysis.googleapis.com
  → artifactregistry.googleapis.com
```

This chain is not configurable. Disabling `compute.googleapis.com` while `cloudbuild` is on causes Cloud Build to fail to schedule workers. We can't avoid the API enablement, so we have to manage the side effect.

The classical risk with the compute default SA was its automatic `roles/editor` grant on the project. That auto-grant is now suppressed by the org-default policy `iam.automaticIamGrantsForDefaultServiceAccounts` (Google began enforcing this on new orgs in Feb 2024). Verified: `gcloud projects get-iam-policy agency-brain-demo` returns no bindings for the SA. So the SA exists but holds no permissions on the project.

## Decision

The compute default SA is **disabled** declaratively in Terraform via `google_project_default_service_accounts`, not deleted.

- Disabled (action = `DISABLE`) — a disabled SA cannot mint OAuth tokens, so even if a future operator binds a role to it, it cannot authenticate. The resource is at `terraform/modules/foundation/iam.tf` (`google_project_default_service_accounts.deprivilege_defaults`); it asserts the disabled state on every apply.
- Not deleted — soft-delete is recoverable for 30 days, and any future `gcloud services enable compute` (which our CI may inadvertently trigger via dependency chains) would resurrect the SA. Disabled is the stable end state; deleted is a transient state that flaps.

A CI step (`scripts/sa_allowlist_check.py`, wired into `cloudbuild.yaml`) asserts on every PR that:
1. Only allowlisted SAs exist as enabled. The allowlist is seeded with the WS-B sync SAs (`asb-sync-airtable-sa@…`, `asb-airtable-sync-invoker@…`) since they already exist on prod.
2. GCP-default SAs in the must-be-disabled list (currently just `*-compute@developer.gserviceaccount.com`) are disabled.

The allowlist grows as workstreams ship their own SAs.

### 2026-04-29 amendment — resource type switch

The original implementation used `google_service_account` with an explicit `account_id = "<project_number>-compute"` and an `import` block in `terraform/envs/prod/main.tf`. **This never applied successfully**: GCP's account_id regex (`^[a-z]([-a-z0-9]*[a-z0-9])?$`) rejects identifiers starting with a digit, so `terraform plan` errored before any apply could run.

The replacement is `google_project_default_service_accounts` with `action = "DISABLE"`. This is the purpose-built resource for managing GCP default SAs — it acts on default SAs by service without requiring us to declare the SA itself, sidestepping the regex issue entirely. The `import` block was removed (the new resource is purely declarative; first apply records the action and exits). The CI allowlist check is unchanged and remains the operational safety net.

## Rationale

- **Defense in depth.** Org policy blocks the Editor auto-grant; disabling the SA blocks token minting; the CI check catches drift. Three independent controls, none load-bearing on its own.
- **Declarative.** The Terraform import means "disabled" is the asserted state, not a one-time gcloud command we have to remember to re-run.
- **Cheap.** No infra cost. CI step adds a few seconds.
- **Reversible.** If we later need a Compute Engine VM (we don't, in v1), we'd add a purpose-built SA per PRD §4.2 and leave the default disabled.

This fits the security-vs-cost trade-off recorded in user memory for this repo (pragmatic defaults for a 2-person tool over PRD's full defense-in-depth).

## Consequences

- The acceptance criterion in `docs/acceptance/ws-a-foundation.md` ("No SAs yet") is amended: the compute default SA is allowed to exist if and only if it is disabled. A new bullet asserts the CI check passes.
- WS-B onwards must add their SAs to `ALLOWED_EMAILS` in `scripts/sa_allowlist_check.py` when they land, otherwise CI will fail.
- If a future workstream needs to *enable* compute (e.g. a Cloud Run VM-backed worker), this ADR must be revisited — the SA might need to be re-enabled and properly scoped, or replaced with a dedicated SA.

## Revisit if

- We start running anything on Compute Engine, GKE, or Cloud Run for VPC connectivity (any of which would legitimately need the compute SA or a replacement).
- Google changes the transitive enablement chain so that `cloudbuild` no longer pulls in `compute`.
- Org policy `iam.automaticIamGrantsForDefaultServiceAccounts` is loosened.
