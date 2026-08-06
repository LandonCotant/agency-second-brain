# WS-G7: Knowledge Surfacer — Acceptance Criteria

Sign-off gate for the Knowledge Surfacer (ADR 0046). PR A1 (module +
tests) → PR A2 (Calendar ingester corpus) → PR A3 (Cloud Run service +
Terraform) → PR A4 (this acceptance + Chat App registration +
PRODUCTION_STATE bump).

**Depends on:** WS-A (foundation), WS-C (agent runtime), ADR 0038
(VECTOR_SEARCH on `agent_outputs.notes`), ADR 0027 (DWD discipline).

## Functional checks

- [ ] **`/ask` returns a grounded answer.** From the `Brain alerts`
  Chat space: `/ask what did I capture about VECTOR_SEARCH?` returns
  a 1-3 sentence answer with `[N]` citation markers and a Sources
  card with clickable Drive links. Latency p95 < 15s.
- [ ] **Calendar corpus is queryable.** `/ask when did I last meet
  with <real attendee>?` returns a calendar-grounded answer with
  the meeting date + summary; the citation references a calendar
  event scope (note_kind='calendar_event').
- [ ] **Hallucination guardrail trips.** `/ask what did Account
  ZZZTOP say about Q2 revenue?` (no such account exists) returns a
  refusal mentioning "couldn't ground" instead of a fabricated
  answer.
- [ ] **HIPAA invariant holds.** Insert a transient row in a sandbox
  table with `hipaa_isolated = TRUE` that would otherwise match a
  test query; verify the runtime guard refuses + emits
  `HIPAA_GUARD_TRIPPED`. Roll back the test row.
- [ ] **Auth refusal works.** A non-authorized Workspace user running
  `/ask` sees "This Brain is configured for one user. (Sorry!)" —
  not a 401, not a partial answer. (Validates the email-allowlist
  refusal path independent of the OIDC check.)
- [ ] **Pro escalation observed.** Force a low-confidence retrieval
  (e.g. ask a question about a topic with very few notes); verify
  the audit log row's `output` summary mentions
  `model=gemini-2.5-pro` (Pro escalation triggered when
  `len(chunks) < 3` OR `confidence < 0.5`).

## Security checks

- [ ] **Surfacer SA has no DWD.** `asb-knowledge-surfacer-sa` does NOT
  appear in `docs/dwd_scopes.md` and has no
  `iam.serviceAccountTokenCreator` bindings. Auth is via Chat OIDC
  + email allowlist, not impersonation. (ADR 0046 §6 + §7 / ADR
  0027 §3 invariant preserved.)
- [ ] **Cloud Run service is NOT public.** The service has
  `roles/run.invoker` granted to `chat@system.gserviceaccount.com`
  only. No `allUsers` binding.
- [ ] **No predefined high-privilege roles.**
  `scripts/least_privilege_check.py` passes — the
  `tbKnowledgeSurfacer` custom role contains only the four
  permissions documented in `terraform/modules/agent_runtime/knowledge_surfacer.tf`.
- [ ] **Allowlist updated.**
  `asb-knowledge-surfacer-sa@agency-brain-demo.iam.gserviceaccount.com`
  is in `scripts/sa_allowlist_check.py::ALLOWED_EMAILS`.
- [ ] **Calendar ingester preserves DWD invariant.**
  `asb-calendar-ingester-sa` impersonates `asb-agent-triage-sa` for
  the EXISTING `calendar.readonly` scope (no new DWD scope added by
  PR A2). ADR 0027 §3 invariant holds.

## Cost checks

- [ ] **Per-query cost ≤ $0.01.** After 5 prod queries:
  ```sql
  SELECT AVG(cost_usd), MAX(cost_usd)
  FROM `agency-brain-demo.agent_audit_log.events`
  WHERE agent_id = 'knowledge-surfacer'
    AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
  ```
  Expected: AVG ≤ $0.01, MAX ≤ $0.05 (Pro escalation).
- [ ] **Monthly burn projection ≤ $5.** With ~10 queries/day expected
  volume, monthly burn ≤ $5 — comfortable in the $50/mo envelope
  (ADR 0024).

## Documentation checks

- [ ] `docs/adr/0046-knowledge-surfacer-model-and-surface.md` is the
  canonical decision document. Supersedes PRD §3 row + §6.6 + §4.4
  (Surfacer Model Armor). Threat model section addresses the
  prompt-injection-without-Model-Armor question.
- [ ] `docs/runbooks/knowledge_surfacer_chat_app_setup.md` walks
  through the manual Chat App registration end-to-end including
  verification queries.
- [ ] `docs/PRODUCTION_STATE.md` deployment table includes the
  Knowledge Surfacer Cloud Run *service* + the Calendar ingester
  Cloud Run Job rows.
- [ ] `docs/ROADMAP.md` reflects WS-G7 as shipped + flags the
  remaining v2 question (Gmail-into-corpus: live-fetch vs
  pre-index).

## v1 limitations (documented, accepted)

- **Gmail not in corpus.** Architecture decision deferred to v2 — see
  ADR 0046 §"Operational notes". The system prompt sets the
  expectation explicitly: "I can only see your notes and calendar —
  for email content, search Gmail directly."
- **Single-user auth.** v1 is the operator-only by exact email match. v2
  multi-user auth is out of scope per PRD §6.6.
- **Cold-start latency.** `min_instance_count = 0` means the first
  query of the day cold-starts (~3-5s). Within Chat's 30s envelope;
  revisit `min_instance_count = 1` (~$5/mo) only if the operator complains.
- **Entity-presence regex is heuristic.** Title Case false positives
  mitigated by an allowlist; missing-entity diagnostics shown to
  the user so the regex stays iterable.

## Sign-off

- [ ] the implementer — date: ____________
