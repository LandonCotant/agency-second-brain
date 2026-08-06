"""Unit tests for bucket_iam_drift pure-logic functions.

The Cloud Storage / IAM I/O surface is covered by Terraform-managed baselines
plus the daily Cloud Run Job. These tests pin the diffing + formatting logic
that decides whether a drift event fires.
"""

from __future__ import annotations

import json
from pathlib import Path

from agency_brain.audit.bucket_iam_drift import (
    _format_diff,
    _is_clean,
    diff_state,
    load_baseline,
)

# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------


def test_load_baseline_returns_empty_when_path_missing(tmp_path: Path):
    """A missing baseline file is treated as 'no expected state' — not an
    error. Keeps the first-run path clean: capture the live state, commit
    it, the next run finds a baseline and starts comparing."""
    missing = tmp_path / "nope.json"
    assert load_baseline(missing) == {}


def test_load_baseline_parses_json(tmp_path: Path):
    baseline = {"bucket-a": {"roles/storage.objectViewer": ["user:owner@example.com"]}}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    assert load_baseline(path) == baseline


# ---------------------------------------------------------------------------
# diff_state
# ---------------------------------------------------------------------------


def test_diff_state_clean_when_actual_matches_baseline():
    state = {"bucket-a": {"roles/storage.admin": ["user:owner@example.com"]}}
    diff = diff_state(state, state)
    assert diff == {"new_buckets": [], "missing_buckets": [], "binding_changes": {}}


def test_diff_state_flags_new_bucket():
    actual = {"bucket-a": {}, "bucket-new": {"roles/storage.admin": ["user:x@y.com"]}}
    baseline = {"bucket-a": {}}
    diff = diff_state(actual, baseline)
    assert diff["new_buckets"] == ["bucket-new"]
    assert diff["missing_buckets"] == []
    assert diff["binding_changes"] == {}


def test_diff_state_flags_missing_bucket():
    actual = {"bucket-a": {}}
    baseline = {"bucket-a": {}, "bucket-gone": {"roles/storage.admin": []}}
    diff = diff_state(actual, baseline)
    assert diff["new_buckets"] == []
    assert diff["missing_buckets"] == ["bucket-gone"]


def test_diff_state_flags_added_principal_on_existing_bucket():
    actual = {
        "bucket-a": {
            "roles/storage.objectViewer": [
                "user:owner@example.com",
                "user:new@example.com",
            ]
        }
    }
    baseline = {"bucket-a": {"roles/storage.objectViewer": ["user:owner@example.com"]}}
    diff = diff_state(actual, baseline)
    assert diff["binding_changes"] == {
        "bucket-a": {
            "added": {"roles/storage.objectViewer": ["user:new@example.com"]},
            "removed": {},
        }
    }


def test_diff_state_flags_removed_principal():
    actual = {"bucket-a": {"roles/storage.objectViewer": ["user:owner@example.com"]}}
    baseline = {
        "bucket-a": {
            "roles/storage.objectViewer": [
                "user:owner@example.com",
                "user:removed@example.com",
            ]
        }
    }
    diff = diff_state(actual, baseline)
    assert diff["binding_changes"] == {
        "bucket-a": {
            "added": {},
            "removed": {"roles/storage.objectViewer": ["user:removed@example.com"]},
        }
    }


def test_diff_state_flags_added_role_on_existing_bucket():
    actual = {
        "bucket-a": {
            "roles/storage.objectViewer": ["user:owner@example.com"],
            "roles/storage.admin": ["user:new-admin@example.com"],
        }
    }
    baseline = {"bucket-a": {"roles/storage.objectViewer": ["user:owner@example.com"]}}
    diff = diff_state(actual, baseline)
    assert diff["binding_changes"] == {
        "bucket-a": {
            "added": {"roles/storage.admin": ["user:new-admin@example.com"]},
            "removed": {},
        }
    }


# ---------------------------------------------------------------------------
# _is_clean
# ---------------------------------------------------------------------------


def test_is_clean_true_when_all_three_categories_empty():
    diff = {"new_buckets": [], "missing_buckets": [], "binding_changes": {}}
    assert _is_clean(diff) is True


def test_is_clean_false_when_any_category_has_drift():
    assert _is_clean({"new_buckets": ["x"], "missing_buckets": [], "binding_changes": {}}) is False
    assert _is_clean({"new_buckets": [], "missing_buckets": ["y"], "binding_changes": {}}) is False
    assert (
        _is_clean({"new_buckets": [], "missing_buckets": [], "binding_changes": {"b": {}}}) is False
    )


# ---------------------------------------------------------------------------
# _format_diff
# ---------------------------------------------------------------------------


def test_format_diff_no_drift_returns_marker():
    assert (
        _format_diff({"new_buckets": [], "missing_buckets": [], "binding_changes": {}})
        == "(no drift)"
    )


def test_format_diff_renders_added_principal():
    diff = {
        "new_buckets": [],
        "missing_buckets": [],
        "binding_changes": {
            "bucket-a": {
                "added": {"roles/storage.admin": ["user:rogue@example.com"]},
                "removed": {},
            }
        },
    }
    out = _format_diff(diff)
    assert "+ bucket-a :: roles/storage.admin -> user:rogue@example.com" in out


def test_format_diff_renders_new_and_missing_buckets():
    diff = {
        "new_buckets": ["bucket-new"],
        "missing_buckets": ["bucket-gone"],
        "binding_changes": {},
    }
    out = _format_diff(diff)
    assert "+ bucket bucket-new" in out
    assert "- bucket bucket-gone" in out
