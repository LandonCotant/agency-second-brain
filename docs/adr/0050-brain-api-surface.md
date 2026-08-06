# ADR 0050 — Brain API surface (Surfacer `POST /api/ask`)

**Status:** **RETIRED in full by ADR 0059, 2026-05-31** — the `/api/ask` route existed only to feed the separate Agent Coordination dashboard, which is now permanently deferred (the Claude app is the canonical interface). Originally Accepted 2026-05-11 (Phase H, WS-G corpus + voice).
Extends ADR 0046 (Knowledge Surfacer).

## Context

The user is building a separate **Agent Coordination dashboard**
(Mac-desktop, single-tenant, Cloudflare Tunnel, Postgres + PostgREST +
Python workers managed by launchd). The dashboard's roadmap calls for
**voice conversation with the Brain via Gemini Live**, where the model
streams audio in/out and uses retrieval as a tool call.

The Brain's existing Surfacer endpoint (`POST /`) is locked to Chat
OIDC: the caller must be the per-project gsuiteaddons SA, and responses
are wrapped in a Workspace Add-ons `hostAppDataAction` envelope. That
contract doesn't extend to a desktop Gemini Live client.

Options considered:

1. **New Cloud Run service** dedicated to the API. Adds a second image,
   second deploy, second IAM surface. Same retrieval + synthesis
   pipeline duplicated.
2. **Live-fetch directly from BQ in the dashboard.** Bypasses the
   Surfacer's guardrails (entity-presence check, refusal heuristics,
   confidence scoring). Couples the dashboard to the Brain's BQ schema.
3. **Second route on the existing Surfacer service.** Reuses the
   agent + retriever + synthesizer; differs only in auth + response
   shape. One image, one deploy.

Option 3 is chosen.

## Decision

### §1 — Second Flask route: `POST /api/ask`

The Surfacer service exposes a new route alongside the existing
`POST /` Chat handler:

```
POST /api/ask
Authorization: Bearer <OIDC ID token>
Content-Type: application/json
Body: {"question": "<free-text>"}

200 Response:
{
  "answer":         "<markdown>",
  "citations":      [{"chunk_number": 1, "filename": "...", "url": "..."}, ...],
  "confidence":     0.82,
  "refused":        false,
  "refusal_reason": null,
  "model_used":     "gemini-2.5-flash",
  "cost_usd":       0.005
}
```

The route reuses `_get_agent()` and `KnowledgeSurfacerAgent.invoke` —
**no separate pipeline**. The Chat-card wrapping is the only Chat-path
work that doesn't apply here; `_serialize_response` produces flat
JSON instead.

### §2 — Dual-caller OIDC

`chat_app.verify_chat_request` is unchanged. The new route calls it
with a different `expected_caller_email`:

| Route       | Expected caller                                                  |
|-------------|------------------------------------------------------------------|
| `POST /`    | `service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` |
| `POST /api/ask` | `asb-brain-api-caller-sa@<project>.iam.gserviceaccount.com`         |

The same JWT verifier + audience check apply to both. The Cloud Run
service IAM `roles/run.invoker` is granted to **both** principals
(only when `brain_api_enabled = true` — see §3).

### §3 — Opt-in via `brain_api_enabled`

A TF boolean controls the entire surface:

- `brain_api_enabled = false` (default): the `BRAIN_API_CALLER_EMAIL`
  env var is unset, so the route returns `404 {"error": "api_disabled"}`
  immediately. The SA + `run.invoker` binding are not provisioned. Net
  effect on existing deploys: zero.
- `brain_api_enabled = true`: TF provisions `asb-brain-api-caller-sa`,
  grants it `run.invoker` on the Surfacer service, grants
  `owner@example.com` resource-scoped
  `iam.serviceAccountTokenCreator` so the dashboard's host can mint
  OIDC ID tokens by impersonating the SA. The Flask route comes online.

### §4 — Auth model + threat surface

The dashboard mints an OIDC ID token by impersonating
`asb-brain-api-caller-sa` (via `gcloud auth print-identity-token
--impersonate-service-account=...` or the equivalent SDK call). The
Surfacer's verifier confirms:

1. Token signature is valid (signed by Google).
2. `aud` claim equals `CHAT_OIDC_AUDIENCE` (the Cloud Run service URL).
3. `email` claim equals `BRAIN_API_CALLER_EMAIL` (the caller SA).

Anyone who can impersonate `asb-brain-api-caller-sa` can query the
Brain. v1 grants impersonation only to
`owner@example.com` (resource-scoped); the dashboard runs
on the user's Mac, so the operator is the only principal in the chain.
This matches the Surfacer's existing single-operator posture.

Cloud Run `roles/run.invoker` is granted to the SA (NOT `allUsers`); a
caller without the SA's impersonation rights gets HTTP 403 from Cloud
Run before reaching the app's OIDC verifier.

### §5 — Response shape (dashboard contract)

The JSON shape is the dashboard-facing contract. Field names are
pinned by `test_serialize_response_pins_field_names`. The Gemini Live
tool definition in the dashboard repo depends on it.

Notably:

- `citations` is a list of flat dicts (`chunk_number`, `filename`,
  `url`). NOT the Chat-card `cardsV2` widget tree.
- `refused` + `refusal_reason` surface the entity-presence guardrail's
  decision. The dashboard's tool wrapper can route these to a
  "this query was refused" branch instead of speaking the refusal.
- `cost_usd` exposes per-query Vertex spend so the dashboard can show
  it on the operator's HUD.

### §6 — Out of scope

- **Rate limiting.** Cloud Run's per-region concurrency cap + the
  Surfacer's `max_instance_count = 3` are the only throttle. A stuck
  retry loop in the dashboard could burn $5-10/hour in Vertex spend.
  If observed in v1, add token-bucket via Memorystore or a simple
  request-log table.
- **Authentication of the human user behind the dashboard.** The Brain
  trusts the dashboard host (operator's Mac) implicitly. The dashboard
  itself authenticates operators via Cloudflare Access + a long-lived
  reader JWT per its own plan; that's outside the Brain's concern.
- **Streaming responses.** Gemini Live streams; this API is one-shot
  request/response. The dashboard's tool wrapper handles the latency
  hiccup by speaking a hold message ("checking your notes...") while
  the call resolves.
- **Multiple caller SAs.** v1 is one dashboard. If a second caller
  emerges, `BRAIN_API_CALLER_EMAIL` becomes a comma-separated list and
  the verifier loops; that's a v1.5 lift.
- **Voice infrastructure itself.** Gemini Live streaming, WebRTC, mic
  handling, audio output — all in the dashboard repo. This ADR only
  defines the Brain's contribution.

## Consequences

- Existing Chat path is unchanged. The Chat verifier signature is
  unchanged; only the route + caller-email param differ.
- Dashboard project gains one external dependency (Brain API
  endpoint URL) and one IAM dependency (caller SA email). Both are
  surfaced via TF output (`brain_api_caller_sa_email`,
  `knowledge_surfacer_service_url`).
- Cost: per-query cost identical to the Chat path (~$0.005 at v1
  volume). Expect ~5-20 queries/day during conversational sessions;
  monthly burn ~$1-3.
- If `brain_api_enabled = false` (current default), nothing on the
  prod surface changes. Flipping the flag is a single targeted apply.

## Supersedence

None. Extends ADR 0046. Does not touch ADR 0027 (no new DWD), 0028
(no new RE), or 0024 (cost stays under guardrails).
