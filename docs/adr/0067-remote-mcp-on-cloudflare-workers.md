# ADR 0067 — Remote brain MCP on Cloudflare Workers (OAuth 2.1)

**Status:** Proposed — 2026-06-12

**Workstream:** MCP surface / access-anywhere
**Related:** ADR 0051 (Brain as MCP substrate — supersedes §2 transport/auth for the remote surface), ADR 0059 (Claude app canonical; Surfacer service retired), ADR 0027 + 0064 (impersonation graph — amends the 0064 expansion cap by one member, §3), ADR 0044 (Drive write via folder-share + impersonation), ADR 0017 (drafts-only carries prompt-injection residual), ADR 0024 (cost guardrails).

## Context

ADR 0051 §2 made the MCP server a **local stdio process** authenticated
via the operator's ADC — zero infra cost, but three tethers: the server
only exists where the repo venv exists (one Mac), stdio has no network
story, and ADC user credentials periodically force
`gcloud auth application-default login` re-runs. The operator wants the
15 brain tools available from the Claude mobile app and claude.ai web —
"anywhere, without constantly reauthing."

claude.ai **custom connectors** are the delivery mechanism: a remote
MCP server registered once on the account appears on phone, web, and
desktop simultaneously. Custom connectors require the MCP spec's
**OAuth 2.1** profile — PKCE (S256), protected-resource metadata
discovery, and dynamic client registration (RFC 7591) or manually
supplied client credentials. Once connected, the client holds refresh
tokens: one consent screen per device, then silent.

Since ADR 0059 retired the Knowledge Surfacer service, the Brain has
**no deployed HTTP surface** — whatever hosts the remote MCP server is
the Brain's only network-facing component.

Three hosting shapes were considered:

1. **All-GCP** — Cloud Run hosts the existing Python server with
   streamable-HTTP transport; OAuth 2.1 implemented in-process
   (MCP Python SDK / FastMCP auth). No rewrite, keyless ambient SA
   identity, one control plane.
2. **Hybrid** — a Cloudflare Worker terminates OAuth
   (`workers-oauth-provider`) and proxies to a locked-down Cloud Run
   origin. Least OAuth code to own; adds a proxy hop and a second
   vendor for half the request path.
3. **All-Cloudflare** — rewrite the server in TypeScript on Workers
   using Cloudflare's first-party MCP toolchain. Best-packaged OAuth
   in the industry; costs a rewrite and an exported GCP SA key
   (Workers have no ambient GCP identity, and Workload Identity
   Federation needs an OIDC issuer Workers don't provide).

## Decision

**Option 3 — full Cloudflare Workers implementation.** Operator
decision 2026-06-12, accepting the rewrite cost and the exported-key
trade-off (mitigations in §3) in exchange for Cloudflare's first-party
MCP + OAuth toolchain and edge hosting at $0/mo.

### §1 — Hosting + transport

- TypeScript Worker at `workers/brain-mcp/` in this repo, deployed
  via `wrangler` to `*.workers.dev` initially (custom domain later if
  wanted).
- **`McpAgent` on a SQLite-backed Durable Object** (`MCP_OBJECT`),
  served via `BrainMCP.serve("/mcp")`. *Amended during implementation:*
  the original plan was the stateless `createMcpHandler()` to avoid
  Durable Objects, but the documented, proven pairing with
  `OAuthProvider` (the official `remote-mcp-github-oauth` template) is
  `McpAgent` + DO, and the DO free tier covers this traffic. Chose the
  proven path over the under-documented stateless helper; the tools
  remain stateless in practice (each call hits BQ fresh).
- A **KV namespace (`OAUTH_KV`)** backs `workers-oauth-provider`'s
  token/client/session storage. Workers + KV + Durable Objects all sit
  in their free tiers at this traffic (2 users) with wide margin.

### §2 — AuthN/AuthZ

- **`workers-oauth-provider`** is the OAuth 2.1 authorization server:
  handles PKCE, DCR (`/register`), `/authorize`, `/token`, and
  protected-resource metadata — everything claude.ai's connector
  flow probes for.
- **Upstream IdP: Google**, via an OAuth client on the
  `agency-brain-demo` project marked **Workspace-internal** to
  `example.com`. Internal status means refresh tokens
  never hit the 7-day testing-mode expiry (the "no constant reauth"
  requirement) and Google refuses logins from outside the domain.
- **Server-side allowlist as second factor of authorization:** after
  the Google callback, the Worker verifies the ID token's verified
  `email` claim against an explicit allowlist
  (`owner@example.com`, +1 future teammate) — not just
  the `hd` claim. Unknown email → 403, no token issued.
- CSRF/state handling, consent-dialog escaping, and CSP per
  Cloudflare's securing-MCP guidance (state tokens in KV with 10-min
  TTL, bound to `__Host-` cookies).

### §3 — GCP data plane: `asb-mcp-sa`, accessed keyless via WIF

> **Amended during implementation (2026-06-14).** The original plan
> exported a `asb-mcp-sa` JSON key into a Worker secret. That is blocked
> by the project org policy `constraints/iam.disableServiceAccountKeyCreation`
> (`enforce: true`, set 2026-04-25) — the guardrail that exists precisely
> to stop downloadable keys, and the same class of credential that leaked
> earlier in this build. Rather than override a project-wide control (a
> boolean constraint can't be scoped to one SA, so disabling it re-enables
> key downloads for *every* SA), the Worker authenticates **keyless via
> Workload Identity Federation**. The `asb-mcp-sa` grant set below is
> unchanged — only how the Worker assumes it changed.

A **`asb-mcp-sa`** with the narrowest grant set that covers the 15
tools (passes `scripts/least_privilege_check.py`; registered in
`scripts/sa_allowlist_check.py`). Mirrors the
`aiops_dashboard_reader_iam.tf` posture — custom project role instead
of predefined roles, table-scoped editors:

| Grant | Resource | Covers |
|---|---|---|
| custom role `tbMcpRemote` (`bigquery.jobs.create`, `bigquery.datasets.get`, `aiplatform.endpoints.predict`) | project | run queries + embeddings for `brain_ask` / `capture_note` |
| `roles/bigquery.dataViewer` | `agent_outputs` + `airtable_replica` datasets | all read tools |
| `roles/bigquery.dataEditor` | tables `agent_outputs.{notes, notes_links, decisions, wins, signal_feedback}` only | the 5 write-tool targets (incl. ADR 0052 synthetic rows + ADR 0053 wikilink edges); no write path to `triaged_items`/`risk_flags`/`routed_events`/briefs/reflections |
| `roles/run.invoker` | `asb-people-sync` job only | `sync_people` via the Jobs `:run` REST API |
| `roles/iam.serviceAccountTokenCreator` | on `asb-agent-triage-sa` | Drive/Docs impersonation for `update_weekly_doc` (ADR 0044 path) |

**ADR 0064 amendment:** 0064 capped further expansion of the
impersonation graph on `asb-agent-triage-sa`. This ADR expands it by
exactly one member (`asb-mcp-sa`), same SA-resource-scoped binding
posture, same rationale as the existing five — the alternative
(granting `asb-mcp-sa` its own Drive identity + folder shares) would
add a second Drive-writing identity, which is worse. The cap
otherwise stands.

**Keyless via Workload Identity Federation.** No GCP key exists
anywhere. The flow (`workers/brain-mcp/src/gcp/auth.ts` +
`terraform/.../mcp_remote_wif.tf`):

1. The Worker self-hosts an **OIDC issuer** at its `workers.dev` origin —
   `/.well-known/openid-configuration` + `/.well-known/jwks.json`, the
   JWKS derived at runtime from `WIF_PRIVATE_KEY` (the Worker's own RSA
   key, *not* a GCP credential — org policy doesn't apply).
2. A **WIF pool + OIDC provider** (`brain-mcp-pool` / `brain-mcp-oidc`)
   trusts that issuer (audience `brain-mcp-worker`).
3. The Worker signs a short-lived JWT → exchanges it at GCP **STS** for a
   federated token → **impersonates `asb-mcp-sa`** via the IAM Credentials
   API (`roles/iam.workloadIdentityUser` on the SA for the pool subject).

Why this is the right posture, not just a workaround:

- **No downloadable key** — satisfies the org policy instead of punching
  a project-wide hole in it.
- **Instant rotation without GCP** — swap the Worker's key + republish the
  JWKS; old tokens stop verifying. No `keys delete` race.
- **Same blast radius bound** on a leak of `WIF_PRIVATE_KEY`: read/write
  the signal corpus, read replicas, fire a people-sync — **no Gmail send,
  no source-table writes, no IAM**, held by the drafts-only invariant
  (PRD §4.7) + per-tool wrappers, exactly as the ADR 0017 residual is.
- The `WIF_PRIVATE_KEY` secret lives **only** as a Cloudflare Worker
  secret — never committed, never in GCP.

The earlier mid-build leak of the OAuth client secret is the cautionary
case-in-point: keyless removes the highest-value leakable artifact from
the data plane entirely.

### §4 — Tool surface

All 16 tools from the stdio server (README at
`src/agency_brain/mcp_server/README.md`) are ported 1:1 — same
names, same descriptions, same per-tool safety wrappers (drafted-status
guards, hash-keyed dedup, scope/note_kind enforcement). Split across
`workers/brain-mcp/src/tools/`:

- **read.ts (7):** `brain_ask` (hybrid vector+keyword RRF, full
  retriever port), `open_risk_flags`, `client_summary`,
  `get_calendar_events`, `related_notes`, `open_drafts`,
  `open_commitments`.
- **write.ts (5):** `capture_note`, `insert_decision`, `insert_win`,
  `mark_decision_status`, `record_feedback` (+ ADR 0052 synthetic notes,
  ADR 0053 wikilink edges).
- **person.ts (3):** `person_summary`, `pending_followups`,
  `sync_people` (Cloud Run Jobs `:run` REST, not a `gcloud` shell-out).
- **docs.ts (1):** `update_weekly_doc` (Drive v3 + Docs v1 REST via the
  `asb-mcp-sa`→`asb-agent-triage-sa` Drive-scoped impersonation).

SQL moves verbatim into parameterized `jobs.query` REST calls (the
`{name,type,value}` param shape matches `clients.py:query_rows`).

### §5 — Rollout + fallback (as executed)

1. Terraform (targeted apply): `asb-mcp-sa` + grants (PR #191), then the
   WIF pool/provider + `workloadIdentityUser` binding.
2. Worker: scaffold → reads → writes/person → `update_weekly_doc`.
   Deployed to `brain-mcp.example.workers.dev`.
3. Manual operator steps: Google OAuth consent screen (Internal) +
   client, `wrangler login`, `WIF_PRIVATE_KEY` keypair, claude.ai
   "Add custom connector".
4. Smoke (verified live through the connector): `open_risk_flags` (BQ)
   and `brain_ask` (embed + hybrid RRF) both return real data via the
   keyless WIF chain.
5. The **local stdio server stays as fallback** until cutover:
   re-sync the connector to surface all 16 tools → share the Briefs +
   Reviews rollup folders with `asb-agent-triage-sa` (ADR 0044; needed
   for `update_weekly_doc`) → point scheduled routines at the remote →
   then remove the `brain` block from `claude_desktop_config.json`.
   Retiring the stdio server is the final cutover step, deferred until
   the routines are migrated.

## Consequences

- The Brain's conversation surface becomes truly ambient — phone, web,
  desktop, one OAuth consent per device, no recurring reauth (Google
  internal app + connector-held refresh tokens + a never-expiring SA
  key under rotation).
- Two control planes: GCP (data, IAM) + Cloudflare (compute, OAuth,
  secrets). `wrangler` deploys live outside Terraform — accepted for a
  single Worker; revisit if the Cloudflare footprint grows.
- ~3–5 days of build (TS port + OAuth wiring + parity tests) vs ~2
  for the all-GCP shape — the premium paid for the toolchain choice.
- A second implementation of the 15 tools now exists; the Python stdio
  server and the Worker can drift. Mitigated by the parity test and by
  treating the Worker as canonical once cutover completes.
- $0/mo marginal (Workers + KV free tiers); ADR 0024 budget untouched.
- Supersedes **ADR 0051 §2** (local stdio + operator ADC) for the
  remote surface. ADR 0051 §1's identity claim — Brain as signal
  substrate behind an MCP seam — is unchanged; this ADR is that seam
  going network-native.
