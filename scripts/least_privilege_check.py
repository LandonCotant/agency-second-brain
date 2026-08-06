#!/usr/bin/env python3
"""Reject any IAM binding that grants a predefined high-privilege role to a service account.

Enforces PRD §4.8 PR security gate, backed by §4.2 ("never grant predefined roles like
roles/editor or roles/bigquery.admin" to service accounts).

Forbidden roles, when bound to a `serviceAccount:` principal:
- roles/owner
- roles/editor
- any role whose basename (text after `roles/`) contains the case-insensitive token
  `admin` — covers both `roles/bigquery.admin` and `roles/iam.serviceAccountAdmin`.

Human allowlist (PRD §4.2):
- `user:` and `group:` principals are allowed to hold any role, including roles/owner.
  The PRD restricts only service-account principals. Humans are skipped entirely.

Resource coverage:
- `*_iam_member` — single member, single role.
- `*_iam_binding` — many members, single role.
- `*_iam_policy` — bindings encoded inside `policy_data` (JSON string); decoded and
  iterated.

Skipped:
- `delete` and `no-op` plan actions (the binding is being removed or is unchanged).

Usage:
    terraform -chdir=terraform/envs/prod plan -out=tfplan.bin
    terraform -chdir=terraform/envs/prod show -json tfplan.bin > tfplan.json
    python scripts/least_privilege_check.py tfplan.json

In CI (cloudbuild.yaml) this runs after `terraform plan`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FORBIDDEN_EXACT = {"roles/owner", "roles/editor"}
ADMIN_TOKEN = re.compile(r"admin", re.IGNORECASE)

IAM_RESOURCE_SUFFIXES = ("_iam_member", "_iam_binding", "_iam_policy")
SKIPPED_ACTIONS = {"no-op", "delete"}


def is_forbidden_role(role: str) -> bool:
    if role in FORBIDDEN_EXACT:
        return True
    basename = role.split("/", 1)[1] if role.startswith("roles/") else role
    return bool(ADMIN_TOKEN.search(basename))


def is_service_account(member: str) -> bool:
    return member.startswith("serviceAccount:")


def _iter_iam_changes(plan: dict):
    """Yield (address, resource_type, actions, after-dict) for every
    IAM-related change that is not a no-op or pure delete.

    ``after`` may be empty/None even on a create/update (some providers
    emit ``after: null`` when the value is computed at apply time). We do
    NOT skip those here — the caller fails closed on them rather than
    letting an unverifiable binding pass the gate."""
    for change in plan.get("resource_changes", []):
        rtype = change.get("type", "")
        if not rtype.endswith(IAM_RESOURCE_SUFFIXES):
            continue
        actions = (change.get("change") or {}).get("actions") or []
        if actions and all(a in SKIPPED_ACTIONS for a in actions):
            continue
        after = (change.get("change") or {}).get("after") or {}
        yield change.get("address", "<unknown>"), rtype, actions, after


def _bindings_from_after(rtype: str, after: dict) -> list[tuple[str, list[str]]]:
    """Return list of (role, members) tuples regardless of resource shape."""
    pairs: list[tuple[str, list[str]]] = []

    if rtype.endswith("_iam_policy"):
        # policy_data is a JSON string; bindings live at the top level.
        raw = after.get("policy_data")
        if raw:
            try:
                policy = json.loads(raw)
            except (TypeError, ValueError):
                return pairs
            for b in policy.get("bindings", []) or []:
                role = b.get("role")
                members = b.get("members") or []
                if role and members:
                    pairs.append((role, list(members)))
        return pairs

    role = after.get("role")
    if not role:
        return pairs
    if rtype.endswith("_iam_binding"):
        members = after.get("members") or []
    else:  # _iam_member
        member = after.get("member")
        members = [member] if member else []
    if members:
        pairs.append((role, list(members)))
    return pairs


_VERIFIABLE_ACTIONS = {"create", "update"}


def find_violations(plan: dict) -> list[str]:
    violations: list[str] = []
    for address, rtype, actions, after in _iter_iam_changes(plan):
        if not after:
            # create/update with no resolved 'after' state — we cannot read
            # the role/members, so we cannot clear it. Fail closed rather
            # than silently skipping (the prior behavior was a bypass: an
            # update from a safe role to roles/editor with after=null passed).
            if any(a in _VERIFIABLE_ACTIONS for a in actions):
                violations.append(
                    f"{address}: IAM change with action(s) {actions} has no "
                    f"'after' state — cannot verify role; failing closed "
                    f"(PRD §4.8)"
                )
            continue
        for role, members in _bindings_from_after(rtype, after):
            if not is_forbidden_role(role):
                continue
            for m in members:
                if not is_service_account(m):
                    # Humans (user:/group:) are allowed any role per PRD §4.2.
                    continue
                violations.append(f"{address}: {role} -> {m} (forbidden by PRD §4.2)")
    return violations


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: least_privilege_check.py <tfplan.json>", file=sys.stderr)
        return 2

    plan_path = Path(sys.argv[1])
    if not plan_path.exists():
        print(f"plan file not found: {plan_path} — skipping (no infra changes)")
        return 0

    plan = json.loads(plan_path.read_text())
    violations = find_violations(plan)

    if violations:
        print("Least-privilege check FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("Least-privilege check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
