// Google login handler — the OAuthProvider defaultHandler (ADR 0067 §2).
//
// /authorize redirects to Google; /callback verifies the user against the
// email allowlist, then completes the MCP client's authorization. The
// approval-dialog step from the GitHub template is intentionally dropped:
// Google shows its own consent and the consent screen is Workspace-Internal,
// so the email allowlist is the authorization gate.

import { Hono } from "hono";
import { getPublicJwk } from "./gcp/auth";
import {
  fetchGoogleToken,
  fetchGoogleUser,
  getGoogleAuthorizeUrl,
} from "./oauth-utils";
import type { AuthRequest, Env, Props } from "./types";

const HOSTED_DOMAIN = "example.com";

const app = new Hono<{ Bindings: Env }>();

// OIDC issuer surface for Workload Identity Federation (ADR 0067 §3).
// GCP fetches openid-configuration -> jwks_uri to verify the Worker's
// self-signed JWTs. Static config; the JWKS is derived from WIF_PRIVATE_KEY.
app.get("/.well-known/openid-configuration", (c) =>
  c.json({
    issuer: c.env.WIF_ISSUER,
    jwks_uri: `${c.env.WIF_ISSUER}/.well-known/jwks.json`,
    response_types_supported: ["id_token"],
    subject_types_supported: ["public"],
    id_token_signing_alg_values_supported: ["RS256"],
  }),
);

app.get("/.well-known/jwks.json", async (c) => c.json({ keys: [await getPublicJwk(c.env)] }));

app.get("/authorize", async (c) => {
  const authReq = await c.env.OAUTH_PROVIDER.parseAuthRequest(c.req.raw);
  // Round-trip the MCP client's authorization request through Google's
  // state param (base64url JSON), recovered verbatim in /callback.
  const state = b64urlEncode(JSON.stringify(authReq));
  const redirectUri = new URL("/callback", c.req.url).href;
  const url = getGoogleAuthorizeUrl({
    clientId: c.env.GOOGLE_CLIENT_ID,
    redirectUri,
    state,
    hostedDomain: HOSTED_DOMAIN,
  });
  return Response.redirect(url, 302);
});

app.get("/callback", async (c) => {
  const stateParam = c.req.query("state");
  const code = c.req.query("code");
  if (!stateParam || !code) return c.text("Missing state or code", 400);

  let authReq: AuthRequest;
  try {
    authReq = JSON.parse(b64urlDecode(stateParam)) as AuthRequest;
  } catch {
    return c.text("Invalid state", 400);
  }

  const redirectUri = new URL("/callback", c.req.url).href;
  const [token, errResp] = await fetchGoogleToken({
    code,
    clientId: c.env.GOOGLE_CLIENT_ID,
    clientSecret: c.env.GOOGLE_CLIENT_SECRET,
    redirectUri,
  });
  if (errResp) return errResp;

  const user = await fetchGoogleUser(token);
  const allowed = (c.env.ALLOWED_EMAILS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  if (!user.email_verified || !allowed.includes(user.email.toLowerCase())) {
    return c.text("Access denied — this Brain is private.", 403);
  }

  const { redirectTo } = await c.env.OAUTH_PROVIDER.completeAuthorization({
    request: authReq,
    userId: user.sub,
    metadata: { label: user.email },
    scope: authReq.scope,
    props: { email: user.email, name: user.name, sub: user.sub } satisfies Props,
  });
  return Response.redirect(redirectTo, 302);
});

export const GoogleHandler = app;

function b64urlEncode(s: string): string {
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(s: string): string {
  const pad = s.length % 4 === 0 ? "" : "=".repeat(4 - (s.length % 4));
  return atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
}
