#!/usr/bin/env python3
"""Assert only allowlisted service accounts exist on the Brain project.

PRD §4.2: no shared service accounts. WS-A creates none of its own. Any SA
that exists on the project must either be in ALLOWED_EMAILS or, if it is a
GCP-default SA created as a side effect of API enablement, must be disabled.

Reads `gcloud iam service-accounts list ... --format=json` from stdin so the
script has no GCP client dependency. Cloud Build's project SA holds
`roles/viewer` (granted in iam.tf), which includes `iam.serviceAccounts.list`.

Usage in CI (cloudbuild.yaml):
    gcloud iam service-accounts list \\
        --project=agency-brain-demo --format=json \\
      | python3 scripts/sa_allowlist_check.py

Exit 0 on success, 1 on violation.
"""

from __future__ import annotations

import json
import sys

# Workstream-owned SAs allowed to exist as ENABLED. Update when a workstream
# lands its SA. WS-A intentionally creates none.
ALLOWED_EMAILS: set[str] = {
    # WS-A foundation — custom Cloud Build SA (replaces the legacy
    # <projnum>@cloudbuild.gserviceaccount.com that Google no longer
    # auto-creates for new projects).
    "asb-cloud-build-sa@agency-brain-demo.iam.gserviceaccount.com",
    # WS-B data pipeline.
    "asb-sync-airtable-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-airtable-sync-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-F security — runtime audit jobs + invoker.
    "asb-audit-sensitive-iso@agency-brain-demo.iam.gserviceaccount.com",
    "asb-audit-sensitive-iam@agency-brain-demo.iam.gserviceaccount.com",
    "asb-audit-drafts-bnd@agency-brain-demo.iam.gserviceaccount.com",
    "asb-audit-bucket-iam@agency-brain-demo.iam.gserviceaccount.com",
    # Nightly Airtable schema-drift audit (W3 hardening, PR #174/#175). The SA
    # was added to terraform + applied but missed from this allowlist, which
    # has failed the sa-allowlist gate since W3 merged — corrected here.
    "asb-audit-airtbl-drift@agency-brain-demo.iam.gserviceaccount.com",
    "asb-runtime-audit-inv@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G1 Triage Agent + Pub/Sub bridge (PR 4d).
    "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-triage-bridge-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-D Chat fan-out worker + scheduler invoker (ADR 0023).
    "asb-routing-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-routing-fanout-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G Morning Brief invoker (ADR 0029) — runtime SA is asb-agent-triage-sa.
    "asb-morning-brief-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G4 Evening Reflection invoker (ADR 0036) — runtime SA is asb-agent-triage-sa.
    "asb-evening-reflection-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G Samsung Notes ingestor (ADR 0031). Job runtime SA + scheduler invoker.
    "asb-notes-ingestor-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-notes-ingestor-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-F daily cost-spend audit (ADR 0030).
    "asb-audit-cost-daily@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G2 Risk Watcher (ADR 0033). Job runtime SA + scheduler invoker.
    "asb-risk-watcher-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-risk-watcher-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G PKM Phase 0b Captures materializer (ADR 0039). Job runtime SA +
    # scheduler invoker. Account-id length cap (≤30) forced -mat- shorthand
    # on the invoker SA.
    "asb-captures-materializer-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-captures-mat-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G PKM Phase 3 Brag Spotter (ADR 0043). Job runtime SA + scheduler
    # invoker. Brag Spotter impersonates asb-agent-triage-sa for gmail.compose;
    # the runtime SA holds BQ access on agent_outputs + audit log.
    "asb-brag-spotter-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-brag-spotter-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G PKM Phase D/G Librarian (ADR 0044, daily-reflection-doc plan).
    # Job runtime SA + scheduler invoker. Librarian classifies + moves Drive
    # files (no DWD; folder-share + ADC) and writes agent_outputs.notes /
    # notes_links + audit log.
    "asb-librarian-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-librarian-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # Personal CRM People Sync (ADR 0057). Job runtime SA + scheduler invoker.
    # Bridges airtable_replica.{accounts,contacts} → Brain/05_GALAXY/
    # {01_ACCOUNTS,02_CONTACTS}/<name>.md via folder-share + ADC (no DWD).
    # Runtime SA reads airtable_replica + agent_outputs, writes audit log.
    "asb-people-sync-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-people-sync-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # WS-G7 Knowledge Surfacer SA removed per ADR 0059 — the Cloud Run service
    # was retired (zero usage; brain_ask MCP tool is the canonical query
    # surface). SA deleted from the project 2026-05-31 via targeted apply.
    # CRM Auto-updater (ADR 0047). Job runtime SA + scheduler invoker.
    # Impersonates asb-agent-triage-sa for {gmail.readonly, gmail.modify}
    # to read secondbrain-labeled emails and apply the secondbrain-processed
    # dedup label. Drafts to Airtable Tasks + Pending Updates fields on
    # Contacts/Accounts.
    "asb-crm-updater-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-crm-updater-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # Calendar ingester (ADR 0046 / Workstream A PR A2). Job runtime SA +
    # scheduler invoker. Impersonates asb-agent-triage-sa for the existing
    # calendar.readonly scope (NO new DWD scope). Writes calendar events
    # into agent_outputs.notes for the Knowledge Surfacer to retrieve.
    "asb-calendar-ingester-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-calendar-ingester-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # AI Ops Dashboard reader (PR #128). External personal dashboard
    # (separate repo, Mac-hosted) — NOT an agent. Reads four datasets
    # (agent_audit_log, agent_outputs, airtable_replica, billing_export)
    # and table-level dataEditor on agent_outputs.decisions only
    # (drafts boundary preserved). No scheduler invoker pair — runs on
    # the operator's Mac via SA key or ADC impersonation.
    "asb-aiops-dashboard-sa@agency-brain-demo.iam.gserviceaccount.com",
    # Remote brain MCP Worker SA (ADR 0067). Cloudflare-hosted MCP server
    # for claude.ai custom connectors — NOT an agent. dataViewer on
    # agent_outputs + airtable_replica; table-level dataEditor on the five
    # write-tool targets (notes, notes_links, decisions, wins,
    # signal_feedback); run.invoker on asb-people-sync; token-creator on
    # asb-agent-triage-sa (Drive). Keyless via Workload Identity Federation
    # (org policy blocks SA keys); no scheduler invoker pair. Allowlisted to
    # reconcile main with live prod: the SA was applied from the 0067 branch
    # ahead of that branch's merge, so main's gate flagged it as
    # unallowlisted drift (blocking all PRs). Full rationale in ADR 0067.
    "asb-mcp-sa@agency-brain-demo.iam.gserviceaccount.com",
    # Commitment extractor (ADR 0069). Daily Job that mines commitments from
    # agent_outputs.notes into agent_outputs.commitments. Custom role
    # tbCommitmentExtractor + dataEditor on agent_outputs/agent_state +
    # dataViewer on airtable_replica. Drafts-only — no DWD, no Gmail, no
    # Airtable writes. Account-id length cap (≤30) forced the -extract- stem.
    # Invoker SA holds run.invoker only.
    "asb-commitment-extract-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-commitment-extract-invoker@agency-brain-demo.iam.gserviceaccount.com",
    # Fact extractor (ADR 0070). Daily Job that mines entity-attribute facts
    # from agent_outputs.notes into the append-only agent_outputs.facts log.
    # Custom role tbFactExtractor + dataEditor on agent_outputs/agent_state +
    # dataViewer on airtable_replica. Drafts-only — no DWD, no Gmail, no
    # Airtable writes. Invoker SA holds run.invoker only.
    "asb-fact-extractor-sa@agency-brain-demo.iam.gserviceaccount.com",
    "asb-fact-extractor-invoker@agency-brain-demo.iam.gserviceaccount.com",
}

# GCP auto-creates these when their parent APIs are enabled. They cannot be
# prevented from being created (transitive enablement) but MUST be held in
# the disabled state. Match by suffix because the leading project number
# changes across environments.
MUST_BE_DISABLED_SUFFIXES: tuple[str, ...] = ("-compute@developer.gserviceaccount.com",)


def find_violations(service_accounts: list[dict]) -> list[str]:
    violations: list[str] = []
    for sa in service_accounts:
        email = sa.get("email", "")
        disabled = bool(sa.get("disabled", False))

        if email in ALLOWED_EMAILS:
            continue

        if any(email.endswith(s) for s in MUST_BE_DISABLED_SUFFIXES):
            if not disabled:
                violations.append(f"GCP-default SA must be disabled but is enabled: {email}")
            continue

        violations.append(f"unallowlisted SA exists on project: {email} (disabled={disabled})")
    return violations


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("sa_allowlist_check: empty input — assuming no SAs.")
        return 0

    sas = json.loads(raw)
    if not isinstance(sas, list):
        print("sa_allowlist_check: expected a JSON array on stdin.", file=sys.stderr)
        return 2

    violations = find_violations(sas)
    if violations:
        print("sa_allowlist_check FAILED (PRD §4.2):")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(f"sa_allowlist_check passed ({len(sas)} SA(s) reviewed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
