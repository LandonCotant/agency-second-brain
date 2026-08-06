"""GCS bucket IAM drift check (ADR 0005 compensating control, daily).

Runs as Cloud Run Job ``asb-audit-bucket-iam-drift`` on a daily Cloud
Scheduler cadence. Lists every Cloud Storage bucket in the brain project,
reads each bucket's IAM policy, and diffs against the checked-in baseline at
``terraform/modules/security/expected/bucket_iam_baseline.json``.

Why this exists: ADR 0005 documented the deviation from PRD §4.6 layer 1
(no custom retention-locked audit log bucket — we rely on default Cloud
Audit Log retention). The compensating control is broader monitoring of
bucket IAM so a new principal acquiring storage access is surfaced quickly.

Modes (parallel to ``hipaa_iam_drift``):
- default: read live policies, diff against baseline, emit one event.
- ``--capture``: print the live state as JSON for committing.
- ``--diff``: print a human-readable diff of live vs. baseline.

Required env vars:
- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL``
- ``BASELINE_PATH`` — defaults to ``/app/security/expected/bucket_iam_baseline.json``
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

log = logging.getLogger("agency_brain.audit.bucket_iam_drift")

DEFAULT_BASELINE_PATH = "/app/security/expected/bucket_iam_baseline.json"


def fetch_live_state(
    project_id: str, storage_client: Any = None
) -> dict[str, dict[str, list[str]]]:
    """Return ``{bucket_name: {role: sorted_members}}`` for every bucket.

    Buckets with no IAM bindings (rare — usually means default-only) appear
    with an empty inner dict so the operator can still detect "bucket
    exists" drift between captures.
    """
    if storage_client is None:
        from google.cloud import storage

        storage_client = storage.Client(project=project_id)

    out: dict[str, dict[str, list[str]]] = {}
    for bucket in storage_client.list_buckets():
        policy = bucket.get_iam_policy(requested_policy_version=3)
        bindings: dict[str, list[str]] = {}
        for binding in policy.bindings:
            role = binding.get("role")
            members = sorted(binding.get("members", []))
            if role and members:
                bindings[role] = members
        out[bucket.name] = bindings
    return out


def load_baseline(path: Path) -> dict[str, dict[str, list[str]]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def diff_state(
    actual: dict[str, dict[str, list[str]]],
    baseline: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    """Compute per-bucket additions/removals.

    Three drift categories tracked separately:
    - ``new_buckets`` — bucket name present in actual, absent from baseline.
    - ``missing_buckets`` — bucket name present in baseline, absent from actual.
    - ``binding_changes`` — bucket exists in both; binding-level diff.
    """
    new_buckets = sorted(set(actual) - set(baseline))
    missing_buckets = sorted(set(baseline) - set(actual))

    binding_changes: dict[str, dict[str, dict[str, list[str]]]] = {}
    for bucket in sorted(set(actual) & set(baseline)):
        added: dict[str, list[str]] = {}
        removed: dict[str, list[str]] = {}
        all_roles = set(actual[bucket]) | set(baseline[bucket])
        for role in sorted(all_roles):
            actual_members = set(actual[bucket].get(role, []))
            baseline_members = set(baseline[bucket].get(role, []))
            if extra := actual_members - baseline_members:
                added[role] = sorted(extra)
            if missing := baseline_members - actual_members:
                removed[role] = sorted(missing)
        if added or removed:
            binding_changes[bucket] = {"added": added, "removed": removed}

    return {
        "new_buckets": new_buckets,
        "missing_buckets": missing_buckets,
        "binding_changes": binding_changes,
    }


def _is_clean(diff: dict[str, Any]) -> bool:
    return not diff["new_buckets"] and not diff["missing_buckets"] and not diff["binding_changes"]


def _format_diff(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    for b in diff["new_buckets"]:
        lines.append(f"+ bucket {b}")
    for b in diff["missing_buckets"]:
        lines.append(f"- bucket {b}")
    for bucket, changes in diff["binding_changes"].items():
        for role, members in changes["added"].items():
            for m in members:
                lines.append(f"+ {bucket} :: {role} -> {m}")
        for role, members in changes["removed"].items():
            for m in members:
                lines.append(f"- {bucket} :: {role} -> {m}")
    return "\n".join(lines) if lines else "(no drift)"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bucket_iam_drift")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Print the live bucket IAM state for committing as the baseline.",
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
        actual = fetch_live_state(project_id)
        sys.stdout.write(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return 0

    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-bucket-iam-drift-sa@{project_id}.iam.gserviceaccount.com",
    )

    if args.diff:
        actual = fetch_live_state(project_id)
        baseline = load_baseline(baseline_path)
        sys.stdout.write(_format_diff(diff_state(actual, baseline)) + "\n")
        return 0

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="bucket-iam-drift",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    try:
        actual = fetch_live_state(project_id)
        baseline = load_baseline(baseline_path)
    except Exception as exc:
        log.exception("bucket_iam_drift failed before completion")
        reporter.error(exc)
        return 1

    diff = diff_state(actual, baseline)
    if _is_clean(diff):
        reporter.ok({"baseline_path": str(baseline_path), "bucket_count": len(actual)})
        return 0

    reporter.drift({"baseline_path": str(baseline_path), "diff": diff})
    return 2


if __name__ == "__main__":
    sys.exit(main())
