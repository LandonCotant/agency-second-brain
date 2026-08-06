// Google OAuth upstream helpers (ADR 0067 §2). Adapted from the
// Cloudflare remote-mcp-github-oauth template's utils.ts for Google's
// JSON token endpoint + OIDC userinfo.

const GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo";

export interface GoogleUser {
  sub: string;
  email: string;
  email_verified: boolean;
  name: string;
  hd?: string;
}

export function getGoogleAuthorizeUrl(opts: {
  clientId: string;
  redirectUri: string;
  state: string;
  hostedDomain?: string;
}): string {
  const url = new URL(GOOGLE_AUTH_URL);
  url.searchParams.set("client_id", opts.clientId);
  url.searchParams.set("redirect_uri", opts.redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", "openid email profile");
  url.searchParams.set("state", opts.state);
  // Workspace-internal apps still benefit from the domain hint.
  if (opts.hostedDomain) url.searchParams.set("hd", opts.hostedDomain);
  url.searchParams.set("prompt", "select_account");
  return url.href;
}

export async function fetchGoogleToken(opts: {
  code: string;
  clientId: string;
  clientSecret: string;
  redirectUri: string;
}): Promise<[string, null] | [null, Response]> {
  const resp = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: opts.code,
      client_id: opts.clientId,
      client_secret: opts.clientSecret,
      redirect_uri: opts.redirectUri,
    }),
  });
  if (!resp.ok) {
    return [null, new Response(`Google token exchange failed: ${await resp.text()}`, { status: 500 })];
  }
  const data = (await resp.json()) as { access_token?: string };
  if (!data.access_token) {
    return [null, new Response("Missing access token from Google", { status: 400 })];
  }
  return [data.access_token, null];
}

export async function fetchGoogleUser(accessToken: string): Promise<GoogleUser> {
  const resp = await fetch(GOOGLE_USERINFO_URL, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!resp.ok) throw new Error(`Google userinfo failed: ${resp.status}`);
  const u = (await resp.json()) as Record<string, unknown>;
  return {
    sub: String(u.sub ?? ""),
    email: String(u.email ?? ""),
    // Google returns this as boolean true or string "true" depending on path.
    email_verified: u.email_verified === true || u.email_verified === "true",
    name: String(u.name ?? u.email ?? ""),
    hd: u.hd ? String(u.hd) : undefined,
  };
}
