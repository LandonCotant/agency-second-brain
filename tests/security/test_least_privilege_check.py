"""Unit tests for scripts/least_privilege_check.py."""

from __future__ import annotations

import json
from pathlib import Path

import least_privilege_check as lpc

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tfplans"


def _plan(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_clean_member_passes():
    assert lpc.find_violations(_plan("clean_member.json")) == []


def test_owner_on_sa_fails():
    violations = lpc.find_violations(_plan("owner_on_sa.json"))
    assert len(violations) == 1
    assert "roles/owner" in violations[0]
    assert "asb-bad-sa" in violations[0]


def test_editor_on_sa_fails():
    violations = lpc.find_violations(_plan("editor_on_sa.json"))
    assert len(violations) == 1
    assert "roles/editor" in violations[0]


def test_lowercase_admin_role_fails():
    violations = lpc.find_violations(_plan("admin_lowercase.json"))
    assert len(violations) == 1
    assert "roles/bigquery.admin" in violations[0]


def test_camelcase_admin_role_fails():
    violations = lpc.find_violations(_plan("admin_camelcase.json"))
    assert len(violations) == 1
    assert "serviceAccountAdmin" in violations[0]


def test_owner_on_human_passes():
    # PRD §4.2 allowlist: user: and group: principals can hold any role.
    assert lpc.find_violations(_plan("owner_on_human.json")) == []


def test_iam_binding_flags_only_service_accounts():
    # The fixture has 2 SA members + 1 user member; user is allowed.
    violations = lpc.find_violations(_plan("iam_binding.json"))
    assert len(violations) == 2
    assert all("roles/editor" in v for v in violations)
    assert all("user:" not in v for v in violations)


def test_iam_policy_data_is_parsed():
    # The fixture's policy_data has roles/owner on one SA + one user.
    # Only the SA should be flagged.
    violations = lpc.find_violations(_plan("iam_policy.json"))
    assert len(violations) == 1
    assert "asb-bad-policy" in violations[0]
    assert "roles/owner" in violations[0]


def test_delete_and_noop_are_skipped():
    # delete actions and no-op actions must not produce violations even when
    # the (before/after) role would otherwise be forbidden.
    assert lpc.find_violations(_plan("delete_action.json")) == []


def test_non_iam_resource_is_ignored():
    # google_iam_workforce_pool does not bind a role/member; must be ignored
    # by the new resource-suffix filter.
    assert lpc.find_violations(_plan("non_iam_resource.json")) == []


def test_empty_plan_passes():
    assert lpc.find_violations({}) == []


def test_create_or_update_with_null_after_fails_closed():
    # A create/update whose 'after' is null (provider computes it at apply)
    # cannot be verified. Previously this was silently skipped — a bypass:
    # an update from a safe role to roles/editor with after=null passed.
    violations = lpc.find_violations(_plan("update_null_after.json"))
    assert len(violations) == 1
    assert "cannot verify" in violations[0]
    assert "google_project_iam_member.computed_after" in violations[0]


def test_is_forbidden_role_unit():
    assert lpc.is_forbidden_role("roles/owner")
    assert lpc.is_forbidden_role("roles/editor")
    assert lpc.is_forbidden_role("roles/bigquery.admin")
    assert lpc.is_forbidden_role("roles/iam.serviceAccountAdmin")
    assert not lpc.is_forbidden_role("roles/viewer")
    assert not lpc.is_forbidden_role("roles/storage.objectViewer")
    assert not lpc.is_forbidden_role("roles/iam.workloadIdentityUser")
