# ADR 0064 — DWD impersonation graph + triage-SA self-token-creator (addendum to 0027)

**Status:** Accepted
**Date:** 2026-06-10
**Workstream:** WS-F (security) — code-review follow-up

**Relationship:** Addendum to ADR 0027 (DWD delegation surface). Does not
supersede it — 0027's core invariant (one DWD-grantable SA, scopes governed
by the doc-driven allowlist) still holds. This ADR documents how the
*impersonation graph around* that SA has grown, confirms it as accepted
risk, and caps further expansion.

## Context

ADR 0027 §2 established `asb-agent-triage-sa` as the single SA with
Domain-Wide Delegation, and stated: "Other agents impersonate this SA —
they don't get DWD themselves." When 0027 was written there were one or two
impersonators. The 2026-06-10 codebase review found the graph has grown
without any ADR recording the expansion, so the CLAUDE.md shorthand
("narrow blast radius") no longer matches reality.

As of 2026-06-10, the principals holding
`roles/iam.serviceAccountTokenCreator` **on `asb-agent-triage-sa`** (the
SA resource itself, not project-wide) are:

| Principal | Why | Source |
|---|---|---|
| `asb-routing-sa` | `gmail.compose` — Gmail draft fan-out | `routing_fanout_iam.tf` |
| `asb-risk-watcher-sa` | `calendar.readonly` — disengagement signal | `risk_watcher_iam.tf` |
| `asb-crm-updater-sa` | `gmail.readonly` + `gmail.modify` (ADR 0047) | `crm_updater.tf` |
| `asb-calendar-ingester-sa` | `calendar.readonly` (ADR 0046) | `calendar_ingester.tf` |
| `asb-brag-spotter-sa` | `gmail.compose` — win-spotting drafts | `brag_spotter.tf` |
| `user:owner@example.com` | ADC impersonation for deploy/admin | `triage_agent_iam.tf` |
| `asb-agent-triage-sa` (itself) | self-impersonation to mint DWD-scoped tokens | `triage_agent_iam.tf` |

Two facts the review surfaced:

1. **The graph is 5 agent SAs + the human + self**, not the "one or two"
   implicit in 0027. Compromising any one of those five agent SAs grants
   the ability to mint tokens for `asb-agent-triage-sa` with its DWD scopes.
   The DWD-grantable SA's effective attack surface is the union of all five
   agents' surfaces.

2. **`asb-agent-triage-sa` holds token-creator on itself**
   (`triage_token_creator_self`). The triage bridge mints DWD-scoped
   credentials at runtime via
   `impersonated_credentials.Credentials(target_principal=<self>)`, which
   requires `signJwt` on the SA's own resource — without the binding,
   `calendar.events.list` and `gmail.drafts.create` both 403 with
   `iam.serviceAccounts.signJwt denied` (observed in the 2026-05-03 first
   scheduled run). This is a genuine GCP requirement of the
   `impersonated_credentials` self-impersonation path, not an over-grant we
   can simply drop.

## Decision

### 1. The current graph is accepted risk for this 2-person tool

The expansion is the *intended* shape of ADR 0027 — agents impersonate the
one DWD SA rather than each getting their own DWD grant, which would be
strictly worse (N Workspace-side delegations to govern instead of one).
Per user-memory `feedback_security_vs_cost.md`, we accept the union-of-
surfaces blast radius rather than build per-agent DWD or a token-broker
indirection that a 2-person tool doesn't warrant. The compensating controls
remain:

- **Scopes are still capped at the doc-driven allowlist** (ADR 0027 §3 /
  ADR 0047) — `{gmail.compose, calendar.readonly, gmail.readonly,
  gmail.modify}` — and `asb-agent-triage-sa` carries
  `gmail.send`/`messages.modify` on *no* path (drafts-only, PRD §4.7,
  enforced by `drafts_static_check.py`, which this review extended to scan
  `scripts/` too).
- Every binding is **SA-resource-scoped**, never project-wide.
- All seven principals are enumerated in `sa_allowlist_check.py`.

### 2. The self-token-creator binding stays, documented

`triage_token_creator_self` is required by the self-impersonation path and
is retained. It is the one binding whose removal was investigated and
rejected (it breaks DWD credential minting). Documenting it here closes the
"undocumented deviation from least-privilege" gap the review flagged.

### 3. Cap further expansion

Adding a **sixth** agent SA to the impersonation graph requires a superseding
or amending ADR that re-justifies the marginal surface. New agents needing
Gmail-draft or Calendar-read should first ask whether they can consume an
existing agent's output instead of impersonating the DWD SA directly.

### 4. Codify impersonation bindings consistently

`crm_updater.tf` and `calendar_ingester.tf` built the
`service_account_id` from a hand-interpolated resource path string instead
of referencing `google_service_account.tb_agent_triage_sa.name` (the form
every other impersonation binding uses). Same resolved value, but the
string form drops the implicit Terraform dependency edge, so the binding
could be planned before the SA exists and drift detection is harder. Both
are changed to the reference form in this PR.

## Consequences

**Positive**
- The CLAUDE.md / ADR 0027 invariant now matches the live graph; the
  audit baseline (ADR 0058 refresh, separate work) can assert the exact
  seven-principal set.
- The self-binding is no longer an unexplained least-privilege exception.
- All five impersonation bindings now share one Terraform idiom.

**Negative / accepted**
- The blast radius of `asb-agent-triage-sa` remains the union of five agent
  SAs. This is accepted, not eliminated. The cap in §3 keeps it from
  silently growing further.

## Rollout

1. Land this PR (ADR + the two TF reference-form fixes).
2. Targeted apply is a no-op against live state (the bindings already exist
   with identical resolved values; only the TF graph representation
   changes): `terraform plan` should show 0 changes for the two
   `*_impersonate_triage` resources, confirming the string→reference swap
   is value-preserving.

## References

- ADR 0027 (DWD delegation surface — the parent decision)
- ADR 0029 (scope expansion to `calendar.readonly`)
- ADR 0046 (Calendar ingester — `calendar.readonly` impersonator)
- ADR 0047 (CRM Auto-updater — `gmail.readonly` + `gmail.modify`)
- ADR 0017 (Model Armor disabled — drafts-only carries the residual risk)
- `feedback_security_vs_cost.md` (pragmatic-security user preference)
- `scripts/sa_allowlist_check.py` (the seven-principal enumeration)
