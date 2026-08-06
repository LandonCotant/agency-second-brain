# Workload Identity Federation for the remote MCP Worker (ADR 0067 §3).
#
# The org policy constraints/iam.disableServiceAccountKeyCreation
# (project-enforced) blocks exporting a asb-mcp-sa key, so the Cloudflare
# Worker authenticates keyless: it self-hosts an OIDC issuer (signs
# short-lived JWTs, publishes a JWKS at its workers.dev URL), this WIF
# provider trusts that issuer, and the federated identity impersonates
# asb-mcp-sa via the IAM Credentials API. No downloadable key anywhere;
# rotation is instant (swap the Worker's signing key + JWKS).
#
# The Worker's private signing key lives only as a Cloudflare Worker
# secret (WIF_PRIVATE_KEY) — it is NOT a GCP credential, so the org
# policy does not apply. A leak is bounded the same way the key approach
# was (drafts-only + per-tool wrappers) AND is revocable without GCP by
# rotating the published JWKS.

resource "google_iam_workload_identity_pool" "brain_mcp" {
  project                   = var.brain_project_id
  workload_identity_pool_id = "brain-mcp-pool"
  display_name              = "Brain MCP Worker"
  description               = "Cloudflare Worker OIDC federation for the remote brain MCP (ADR 0067). Keyless — org policy blocks SA keys."
}

resource "google_iam_workload_identity_pool_provider" "brain_mcp" {
  project                            = var.brain_project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.brain_mcp.workload_identity_pool_id
  workload_identity_pool_provider_id = "brain-mcp-oidc"
  display_name                       = "brain-mcp Worker OIDC"

  # Map the Worker's single self-asserted subject through to google.subject.
  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }

  oidc {
    # The Worker serves /.well-known/openid-configuration + the JWKS at
    # this origin. GCP fetches the JWKS here to verify the Worker's JWT.
    issuer_uri        = "https://brain-mcp.example.workers.dev"
    allowed_audiences = ["brain-mcp-worker"]
  }
}

# Let the federated identity (the Worker's single subject "brain-mcp")
# impersonate asb-mcp-sa. workloadIdentityUser is the binding the
# STS-token -> generateAccessToken impersonation flow requires.
resource "google_service_account_iam_member" "mcp_wif_user" {
  service_account_id = google_service_account.tb_mcp_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.brain.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.brain_mcp.workload_identity_pool_id}/subject/brain-mcp"
}
