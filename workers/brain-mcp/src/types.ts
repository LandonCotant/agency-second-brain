// Shared types for the remote brain MCP Worker (ADR 0067).

export interface Env {
  // Bindings
  OAUTH_KV: KVNamespace;
  MCP_OBJECT: DurableObjectNamespace;
  // Injected by @cloudflare/workers-oauth-provider into handlers.
  OAUTH_PROVIDER: OAuthHelpers;

  // Vars (wrangler.jsonc)
  BRAIN_PROJECT_ID: string;
  BRAIN_VERTEX_LOCATION: string;
  BRAIN_OUTPUTS_DATASET: string;
  ALLOWED_EMAILS: string;
  BRAIN_BRIEFS_FOLDER_ID: string;
  BRAIN_REFLECTIONS_FOLDER_ID: string;
  BRAIN_REVIEWS_FOLDER_ID: string;

  // Workload Identity Federation (ADR 0067 §3) — keyless GCP auth.
  WIF_ISSUER: string;
  WIF_AUDIENCE: string;
  WIF_PROVIDER: string;
  TB_MCP_SA_EMAIL: string;
  // SA impersonated for Drive/Docs writes (update_weekly_doc), ADR 0044.
  BRAIN_DRIVE_IMPERSONATION_SA: string;

  // Secrets (wrangler secret put)
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  // Worker's own RSA signing key (PKCS8 PEM) for the WIF OIDC JWT. NOT a
  // GCP key — self-managed, rotatable via the published JWKS (ADR 0067 §3).
  WIF_PRIVATE_KEY: string;
}

// Auth context produced by the Google login flow, encrypted into the
// OAuth token and surfaced as `this.props` inside the McpAgent.
export interface Props extends Record<string, unknown> {
  email: string;
  name: string;
  sub: string;
}

// Minimal shape of the OAuthHelpers binding we use. The library types it
// fully; we only touch these three methods.
export interface OAuthHelpers {
  parseAuthRequest(request: Request): Promise<AuthRequest>;
  completeAuthorization(opts: {
    request: AuthRequest;
    userId: string;
    metadata?: Record<string, unknown>;
    scope: string[];
    props: Props;
  }): Promise<{ redirectTo: string }>;
}

export interface AuthRequest {
  responseType: string;
  clientId: string;
  redirectUri: string;
  scope: string[];
  state: string;
  codeChallenge?: string;
  codeChallengeMethod?: string;
}
