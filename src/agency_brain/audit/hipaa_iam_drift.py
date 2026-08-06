"""Brain-project IAM drift check (PRD §4.1 layer 1, daily).

Runs as Cloud Run Job ``asb-audit-sensitive-iam-drift`` on a daily Cloud Scheduler
cadence. Reads the brain project's IAM policy via
``cloudresourcemanager.projects.getIamPolicy`` and compares it against the
checked-in baseline at
``terraform/modules/security/expected/brain_iam_baseline.json``. Any binding
present in actual but not baseline (or vice-versa) emits ``SECURITY_DRIFT``.

What this catches:
- A new IAM binding granted out-of-band (someone clicked "Grant Access" in
  the console without the corresponding TF change).
- A removed binding that the audit baseline still expects (probable
  out-of-band revoke; should be paired with a TF/baseline update).

What this does NOT catch (known gaps, documented in
``docs/runbooks/runtime_audit_response.md``):
- **Cross-project HIPAA assertion** — PRD §4.1 layer 1 requires verifying
  Brain SAs are absent from the HIPAA project's IAM. Doing that here would
  require granting this audit SA ``resourcemanager.projects.getIamPolicy``
  on a peer project, which contradicts the least-privilege boundary the SA
  topology was designed around. Quarterly manual ``gcloud asset
  search-all-iam-policies --scope=organizations/<org>`` covers this.
- **DWD scope grants** in Workspace admin — Workspace SDK access is a
  separate identity boundary; PR #2 only covers GCP IAM.

Modes:
- default: read live policy, diff against baseline, emit one event.
- ``--capture``: print the live policy as JSON to stdout in the baseline
  file format. Operator runs once at first deploy and after every TF change
  that intentionally alters IAM, then commits the result.
- ``--diff``: print a human-readable diff of live vs. baseline; no audit
  event emitted. For interactive operator use during incident response.

Required env vars (set by the Cloud Run Job in
``terraform/modules/security/runtime_audits.tf``):

- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL``
- ``BASELINE_PATH`` — defaults to ``/app/security/expected/brain_iam_baseline.json``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from ..common.audit_log import AuditLogClient
from ._reporter import AuditScriptContext, DriftReporter

log = logging.getLogger("agency_brain.audit.hipaa_iam_drift")

DEFAULT_BASELINE_PATH = "/app/security/expected/brain_iam_baseline.json"


def fetch_live_policy(project_id: str, rm_client: Any = None) -> dict[str, list[str]]:
    """Return ``{role: sorted_members}`` for the live brain project IAM.

    Members are sorted to keep the diff stable across runs. Roles with no
    members are dropped (Cloud Resource Manager occasionally returns these).
    """
    if rm_client is None:
        from google.cloud.resourcemanager_v3 import ProjectsClient

        rm_client = ProjectsClient()
    policy = rm_client.get_iam_policy(resource=f"projects/{project_id}")
    out: dict[str, list[str]] = {}
    for binding in policy.bindings:
        members = sorted(binding.members)
        if members:
            out[binding.role] = members
    return out


def load_baseline(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def diff_policies(
    actual: dict[str, list[str]], baseline: dict[str, list[str]]
) -> dict[str, dict[str, list[str]]]:
    """Compute additions and removals per role.

    Returns ``{"added": {role: [members]}, "removed": {role: [members]}}``.
    A role removed entirely shows up as ``removed[role] = baseline[role]``;
    a role added entirely shows up as ``added[role] = actual[role]``.
    """
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    all_roles = set(actual) | set(baseline)
    for role in sorted(all_roles):
        actual_members = set(actual.get(role, []))
        baseline_members = set(baseline.get(role, []))
        if extra := actual_members - baseline_members:
            added[role] = sorted(extra)
        if missing := baseline_members - actual_members:
            removed[role] = sorted(missing)
    return {"added": added, "removed": removed}


def _is_clean(diff: dict[str, dict[str, list[str]]]) -> bool:
    return not diff["added"] and not diff["removed"]


def _format_diff(diff: dict[str, dict[str, list[str]]]) -> str:
    lines: list[str] = []
    for role, members in diff["added"].items():
        for m in members:
            lines.append(f"+ {role} -> {m}")
    for role, members in diff["removed"].items():
        for m in members:
            lines.append(f"- {role} -> {m}")
    return "\n".join(lines) if lines else "(no drift)"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hipaa_iam_drift")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Print the live IAM policy as JSON for committing as the baseline.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print a human-readable diff and exit; no audit event emitted.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    project_id = os.environ["BRAIN_PROJECT_ID"]
    baseline_path = Path(os.environ.get("BASELINE_PATH", DEFAULT_BASELINE_PATH))

    if args.capture:
        actual = fetch_live_policy(project_id)
        sys.stdout.write(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return 0

    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-sensitive-iam-drift-sa@{project_id}.iam.gserviceaccount.com",
    )

    if args.diff:
        actual = fetch_live_policy(project_id)
        baseline = load_baseline(baseline_path)
        sys.stdout.write(_format_diff(diff_policies(actual, baseline)) + "\n")
        return 0

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="hipaa-iam-drift",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    try:
        actual = fetch_live_policy(project_id)
        baseline = load_baseline(baseline_path)
    except Exception as exc:
        log.exception("hipaa_iam_drift failed before completion")
        reporter.error(exc)
        return 1

    diff = diff_policies(actual, baseline)
    if _is_clean(diff):
        reporter.ok({"baseline_path": str(baseline_path), "role_count": len(actual)})
        return 0

    reporter.drift({"baseline_path": str(baseline_path), "diff": diff})
    return 2


if __name__ == "__main__":
    sys.exit(main())
