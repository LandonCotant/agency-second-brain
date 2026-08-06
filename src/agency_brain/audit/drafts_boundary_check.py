"""Drafts-boundary runtime check (PRD §4.7, nightly).

Runs as Cloud Run Job ``asb-audit-drafts-boundary`` on a nightly Cloud
Scheduler cadence. Two independent assertions, one audit row per run:

1. **Forbidden-role check (IAM).** Reads the brain project's IAM policy
   and verifies no service-account principal holds a forbidden role
   (anything in ``scripts/least_privilege_check.py``'s deny set:
   ``roles/owner``, ``roles/editor``, or any role whose basename
   contains ``admin``).

   Reuses :func:`is_forbidden_role` and :func:`is_service_account` from
   ``scripts/least_privilege_check.py`` (made importable via the
   ``pythonpath = ["src", "scripts"]`` entry in ``pyproject.toml``) so
   the runtime check stays in lock-step with the PR-time gate.

2. **DWD scope allowlist (Workspace).** Parses the granted-scopes table
   in ``docs/dwd_scopes.md`` (the source of truth per PRD §4.3) and
   asserts every row's scope is in :data:`_ALLOWED_DWD_SCOPES`. The
   doc-driven design is documented in ADR 0027.

Either check finding violations → ``reporter.drift({...})`` with both
violation lists in the payload. Both clean → ``reporter.ok(...)`` with
counts of what was reviewed.

Note on WS-D Chat fan-out (ADR 0023): the routing fan-out worker posts
messages to the ``Brain alerts`` Chat space via an incoming webhook (URL
in Secret Manager). Webhook delivery is IAM-invisible — no SA acquires
``chat.spaces.write`` or any Chat OAuth scope, and no role binding
appears in the project IAM policy for the Chat path. This script's
existing assertions remain sufficient for the Chat lane.

Required env vars:
- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL``
- ``DWD_SCOPES_DOC`` (optional; defaults to repo-relative
  ``docs/dwd_scopes.md`` resolved from this file's location)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

# scripts/ is on the import path via pyproject.toml's pythonpath = ["src", "scripts"].
from least_privilege_check import is_forbidden_role, is_service_account

from ..common.audit_log import AuditLogClient
from ._reporter import AuditScriptContext, DriftReporter
from .hipaa_iam_drift import fetch_live_policy

log = logging.getLogger("agency_brain.audit.drafts_boundary_check")

# Per ADR 0027 (extended by ADR 0029, ADR 0047): the DWD allowlist is the
# strict set below. Adding a new scope requires a superseding ADR.
#   - gmail.compose      — WS-G1 Triage drafts + WS-G3 Morning Brief drafts
#   - calendar.readonly  — WS-G3 Morning Brief reads today's calendar
#   - gmail.readonly     — CRM Auto-updater reads `secondbrain`-labeled
#                          email bodies (ADR 0047)
#   - gmail.modify       — CRM Auto-updater applies `secondbrain-processed`
#                          dedup label (ADR 0047). The PR-gate static
#                          check at scripts/drafts_static_check.py
#                          continues to forbid users.messages.send.
_ALLOWED_DWD_SCOPES: frozenset[str] = frozenset(
    {"gmail.compose", "calendar.readonly", "gmail.readonly", "gmail.modify"}
)

# Repo-relative default for docs/dwd_scopes.md. Resolved from this module's
# path (src/agency_brain/audit/drafts_boundary_check.py → repo root → docs/).
_DEFAULT_DWD_DOC = Path(__file__).resolve().parents[3] / "docs" / "dwd_scopes.md"

# Markdown table row in dwd_scopes.md:
#   | `asb-agent-triage-sa` | `gmail.compose` | ... | 2026-05-01 | WS-G1 |
# Header + separator rows (containing "---" or "Service Account") are skipped.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def find_violations(policy: dict[str, list[str]]) -> list[dict[str, str]]:
    """Return one ``{role, member}`` dict per forbidden binding.

    Mirrors the PR-time check's signature shape (a list of strings) but
    returns structured rows so the audit summary can show the operator
    exactly which (role, principal) pair triggered the alert.
    """
    violations: list[dict[str, str]] = []
    for role, members in policy.items():
        if not is_forbidden_role(role):
            continue
        for m in members:
            if is_service_account(m):
                violations.append({"role": role, "member": m})
    return violations


def parse_dwd_scopes(doc_text: str) -> list[dict[str, str]]:
    """Parse the granted-scopes table out of ``docs/dwd_scopes.md``.

    Returns a list of ``{"sa", "scope"}`` dicts — one per data row. Empty
    cells (header separators, the ``_(none yet)_`` placeholder) are
    skipped. Ignores any prose outside the table; only Markdown table
    rows are interpreted.
    """
    rows: list[dict[str, str]] = []
    for line in doc_text.splitlines():
        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) < 2:
            continue
        sa, scope = cells[0], cells[1]
        # Skip header ("Service Account") and separator ("---") rows.
        if not sa or sa.lower().startswith("service account") or set(sa) <= {"-", ":"}:
            continue
        # Skip placeholder rows like "_(none yet)_".
        if sa.startswith("_(") or scope.startswith("_("):
            continue
        rows.append({"sa": _strip_md(sa), "scope": _strip_md(scope)})
    return rows


def find_dwd_violations(scopes: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return one violation dict per row whose scope is not on the allowlist."""
    return [
        {"sa": row["sa"], "scope": row["scope"]}
        for row in scopes
        if row["scope"] not in _ALLOWED_DWD_SCOPES
    ]


def _strip_md(cell: str) -> str:
    """Trim Markdown formatting (backticks, bold) from a table cell."""
    return cell.strip().strip("`*_")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-drafts-boundary-sa@{project_id}.iam.gserviceaccount.com",
    )
    dwd_doc_path = Path(os.environ.get("DWD_SCOPES_DOC", str(_DEFAULT_DWD_DOC)))

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="drafts-boundary",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    try:
        policy = fetch_live_policy(project_id)
        iam_violations = find_violations(policy)
        dwd_scopes = parse_dwd_scopes(dwd_doc_path.read_text(encoding="utf-8"))
        dwd_violations = find_dwd_violations(dwd_scopes)
    except Exception as exc:
        log.exception("drafts_boundary_check failed before completion")
        reporter.error(exc)
        return 1

    if iam_violations or dwd_violations:
        reporter.drift(
            {
                "iam_violations": iam_violations,
                "dwd_violations": dwd_violations,
                "dwd_scopes_checked": len(dwd_scopes),
            }
        )
        return 2

    reporter.ok(
        {
            "sa_principals_checked": _count_service_accounts(policy),
            "dwd_scopes_checked": len(dwd_scopes),
        }
    )
    return 0


def _count_service_accounts(policy: dict[str, list[str]]) -> int:
    seen: set[str] = set()
    for members in policy.values():
        for m in members:
            if is_service_account(m):
                seen.add(m)
    return len(seen)


if __name__ == "__main__":
    sys.exit(main())
