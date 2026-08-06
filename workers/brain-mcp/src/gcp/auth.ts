// GCP auth for Workers via Workload Identity Federation (ADR 0067 §3).
//
// The org policy blocks downloadable SA keys, so the Worker authenticates
// keyless:
//   1. Sign a short-lived OIDC JWT with the Worker's own RSA key
//      (WIF_PRIVATE_KEY secret); the matching public key is published at
//      /.well-known/jwks.json (see getPublicJwk + google-handler.ts).
//   2. Exchange it at GCP STS for a federated token (the WIF provider
//      trusts our issuer).
//   3. Impersonate asb-mcp-sa via the IAM Credentials API.
// The resulting SA access token is cached for the isolate's lifetime.

import type { Env } from "../types";

const SCOPE = "https://www.googleapis.com/auth/cloud-platform";
export const WIF_KID = "brain-mcp-1";

const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive";

let cachedToken: { token: string; exp: number } | null = null;
let cachedDriveToken: { token: string; exp: number } | null = null;
let cachedSigningKey: CryptoKey | null = null;
let cachedPublicJwk: Record<string, string> | null = null;

export async function getAccessToken(env: Env): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  if (cachedToken && cachedToken.exp - 60 > now) return cachedToken.token;

  const jwt = await signOidcJwt(env, now);
  const federated = await stsExchange(env, jwt);
  const sa = await impersonate(env, federated);
  cachedToken = sa;
  return sa.token;
}

// Drive-scoped token via a SECOND impersonation hop (ADR 0044 path):
// asb-mcp-sa (from WIF) -> impersonate asb-agent-triage-sa with the Drive
// scope. asb-mcp-sa holds tokenCreator on the triage SA (PR #191). Used by
// update_weekly_doc only.
export async function getDriveToken(env: Env): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  if (cachedDriveToken && cachedDriveToken.exp - 60 > now) return cachedDriveToken.token;
  const saToken = await getAccessToken(env);
  const url =
    `https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/` +
    `${env.BRAIN_DRIVE_IMPERSONATION_SA}:generateAccessToken`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${saToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ scope: [DRIVE_SCOPE], lifetime: "3600s" }),
  });
  if (!resp.ok) {
    throw new Error(`gcp Drive impersonation failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as { accessToken: string; expireTime: string };
  cachedDriveToken = {
    token: data.accessToken,
    exp: Math.floor(new Date(data.expireTime).getTime() / 1000),
  };
  return cachedDriveToken.token;
}

// Public JWK derived from the signing key — served at /.well-known/jwks.json
// so GCP can verify the Worker's JWTs.
export async function getPublicJwk(env: Env): Promise<Record<string, string>> {
  if (cachedPublicJwk) return cachedPublicJwk;
  const key = await getSigningKey(env);
  const full = (await crypto.subtle.exportKey("jwk", key)) as JsonWebKey;
  cachedPublicJwk = {
    kty: full.kty as string,
    n: full.n as string,
    e: full.e as string,
    alg: "RS256",
    use: "sig",
    kid: WIF_KID,
  };
  return cachedPublicJwk;
}

async function signOidcJwt(env: Env, iat: number): Promise<string> {
  const header = { alg: "RS256", typ: "JWT", kid: WIF_KID };
  const claims = {
    iss: env.WIF_ISSUER,
    sub: "brain-mcp",
    aud: env.WIF_AUDIENCE,
    iat,
    exp: iat + 3600,
  };
  const input = `${b64urlJson(header)}.${b64urlJson(claims)}`;
  const key = await getSigningKey(env);
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(input),
  );
  return `${input}.${b64url(new Uint8Array(sig))}`;
}

async function stsExchange(env: Env, subjectToken: string): Promise<string> {
  const resp = await fetch("https://sts.googleapis.com/v1/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:token-exchange",
      audience: env.WIF_PROVIDER,
      scope: SCOPE,
      requested_token_type: "urn:ietf:params:oauth:token-type:access_token",
      subject_token: subjectToken,
      subject_token_type: "urn:ietf:params:oauth:token-type:jwt",
    }),
  });
  if (!resp.ok) {
    throw new Error(`gcp STS exchange failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as { access_token: string };
  return data.access_token;
}

async function impersonate(
  env: Env,
  federatedToken: string,
): Promise<{ token: string; exp: number }> {
  const url =
    `https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/` +
    `${env.TB_MCP_SA_EMAIL}:generateAccessToken`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${federatedToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ scope: [SCOPE], lifetime: "3600s" }),
  });
  if (!resp.ok) {
    throw new Error(`gcp SA impersonation failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as { accessToken: string; expireTime: string };
  return { token: data.accessToken, exp: Math.floor(new Date(data.expireTime).getTime() / 1000) };
}

async function getSigningKey(env: Env): Promise<CryptoKey> {
  if (cachedSigningKey) return cachedSigningKey;
  if (!env.WIF_PRIVATE_KEY) throw new Error("WIF_PRIVATE_KEY secret is not set");
  const der = pemToDer(env.WIF_PRIVATE_KEY, "PRIVATE KEY");
  // extractable=true so getPublicJwk can export the public components.
  cachedSigningKey = await crypto.subtle.importKey(
    "pkcs8",
    der,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    true,
    ["sign"],
  );
  return cachedSigningKey;
}

function pemToDer(pem: string, label: string): Uint8Array {
  const body = pem
    .replace(`-----BEGIN ${label}-----`, "")
    .replace(`-----END ${label}-----`, "")
    .replace(/\s+/g, "");
  return base64ToBytes(body);
}

function b64urlJson(obj: unknown): string {
  return b64url(new TextEncoder().encode(JSON.stringify(obj)));
}

function b64url(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
