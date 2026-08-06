# ADR 0056 — Retire Cloud Run morning-brief + evening-prompt schedulers

**Status:** Accepted — 2026-05-18. Supersedes ADR 0029 (Morning Brief topology) for the scheduler/surface decision only — the Cloud Run Job, SA, IAM, and BQ destinations stay deployed. Supersedes ADR 0040 §1 (Evening Reflection PROMPT mode) for the same surface-level scope; REFLECT mode (ADR 0040 §2) is untouched.

## Context

ADR 0029 (Morning Brief) and ADR 0040 §1 (Evening Reflection PROMPT) both ship as Cloud Run Jobs that compose a daily Gmail draft — Morning Brief at 7:25 AM PT, Evening PROMPT at 4 PM PT. They were the right v1 surface: scheduled compute, deterministic synthesis, Gmail as the read-surface.

In May 2026 the user shipped two Claude Code Local routines that cover the same use case from a richer surface:

- **`Morning brief daily`** (Local, ~6:37 AM PT) — calls Brain MCP tools (`get_calendar_events`, `open_risk_flags`, `brain_ask`, `client_summary`) and writes to the consolidated Drive "Morning Briefs" Doc via `update_weekly_doc(kind="brief")`. The Drive Doc is read on desktop; the Gmail draft is unread.
- **`Daily evening reflection`** (Local, ~6 PM PT weekdays) — calls Brain MCP (`brain_ask`, `open_risk_flags`) for personalized context, writes to the consolidated Drive "Evening Reflections" Doc with reflection prompts. The 4 PM Cloud Run Gmail draft is generic by comparison and unread.

The Cloud Run Jobs' Gmail drafts have become unread inbox traffic. They duplicate work that's already happening on a better surface, and they contribute to the alert-noise problem that ADR 0055 just addressed for the hipaa-isolation audit.

## Decision

Pause both scheduler triggers via Terraform. The Cloud Run Jobs, service accounts, custom IAM, BQ destinations, and images all stay deployed. Re-enabling either is a one-line change + targeted apply.

Implementation:

- `terraform/modules/agent_runtime/morning_brief.tf` — `google_cloud_scheduler_job.tb_morning_brief_daily` gains `paused = true` (no prior paused field). Description updated to reference this ADR.
- `terraform/modules/agent_runtime/evening_reflection.tf` — `google_cloud_scheduler_job.tb_evening_prompt_daily` already had `paused = true` as the initial value + `lifecycle { ignore_changes = [paused] }` (the original "deploy paused, unpause via gcloud after smoke" pattern from PR-C). The `ignore_changes` block is **removed** so TF actively enforces the retired state. Description updated.

The REFLECT scheduler (`tb_evening_reflection_daily`, 9 PM PT) is **untouched** — it remains the canonical voice-memo processor (ADR 0040 §2). Its `ignore_changes = [paused]` stays in place since manual control is still appropriate there.

## Why not delete the Cloud Run Jobs entirely

The Jobs cost ~$0 at rest (Cloud Run v2 has no idle charge), the IAM/SA bindings are tiny, and the image is already built. Keeping them deployed:

- Lets a one-line `paused = false` + apply revert the decision if the Local routines turn out to be unreliable (e.g., laptop closed at 6:37 AM and the brief never fires).
- Preserves the historical `agent_outputs.morning_briefs` BQ rows (no schema change, no DELETE).
- Avoids the destroy/create churn that would force IAM re-grants.

If both Local routines run stably for 30+ days without falling back to the Cloud Run versions, the next ADR can retire the Jobs themselves (terraform destroy of `google_cloud_run_v2_job.tb_morning_brief` + `google_cloud_run_v2_job.tb_evening_reflection`'s PROMPT-mode logic). Not now.

## Alternatives rejected

**Re-route the Cloud Run Gmail drafts to a different read surface.** Don't need them; the Local routines write to Drive which is where the user actually reads.

**Hard-disable via `enabled = false` on the Cloud Run Job resource.** That blocks the Job from being invoked even manually; pausing the scheduler is the lower-risk path (Job still works for manual smoke fires).

**Keep both surfaces firing — let the user pick which to read.** That's what's been happening. The friction is real (two surfaces means doubled cognitive load + the unread Gmail drafts contribute to the same noise problem ADR 0055 addressed). Pick one canonical surface.

## Re-enable checklist

If a Local routine fails repeatedly (laptop closed, MCP server down, etc.) and the Cloud Run surface needs to come back:

- [ ] For Morning Brief: set `paused = false` in `terraform/modules/agent_runtime/morning_brief.tf:tb_morning_brief_daily`. `terraform apply -target='module.agent_runtime.google_cloud_scheduler_job.tb_morning_brief_daily'`.
- [ ] For Evening PROMPT: set `paused = false` in `terraform/modules/agent_runtime/evening_reflection.tf:tb_evening_prompt_daily`. Optionally restore the `lifecycle { ignore_changes = [paused] }` block if you want to revert to manual control. `terraform apply -target='module.agent_runtime.google_cloud_scheduler_job.tb_evening_prompt_daily'`.
- [ ] Verify state: `gcloud scheduler jobs describe <name> --location=us-central1` returns `state: ENABLED`.
- [ ] Update `docs/PRODUCTION_STATE.md` to remove the PAUSED markers.

## Related

- ADR 0029 — Morning Brief topology (still authoritative for the Cloud Run Job + SA + IAM; only the scheduler/Gmail-draft surface is retired)
- ADR 0040 §1 — Evening Reflection PROMPT mode (same scope)
- ADR 0040 §2 — Evening Reflection REFLECT mode (untouched, canonical 9 PM voice-memo processor)
- ADR 0055 — Same shape: pause a scheduler whose downstream surface is unused
- PR #147 (`update_weekly_doc`) — the MCP tool the Local routines call to write to the consolidated Drive Docs
