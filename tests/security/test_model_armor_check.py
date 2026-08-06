"""Unit tests for `scripts/model_armor_check.py`.

The gate is RE-keyed: any `google_vertex_ai_reasoning_engine` matching a
required-armor agent name pattern must have a covering
`google_model_armor_template` resource (template_id matching the same
pattern). Templates that exist must have a non-empty filter_config.
"""

from __future__ import annotations

from pathlib import Path

import model_armor_check as mac

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "terraform"


_TMP_CACHE: dict[tuple[str, ...], Path] = {}


def _make_tmp(names: list[str]) -> Path:
    import shutil
    import tempfile

    key = tuple(sorted(names))
    if key in _TMP_CACHE:
        return _TMP_CACHE[key]

    tmp = Path(tempfile.mkdtemp(prefix="asb-armor-test-"))
    for name in names:
        shutil.copy(FIXTURES / name, tmp / name)
    _TMP_CACHE[key] = tmp
    return tmp


def _scan_files(names: list[str]) -> list[str]:
    return mac.find_violations(_make_tmp(names))


# ---------------------- pass cases ----------------------


def test_triage_with_armor_passes() -> None:
    """Triage RE + matching Template with real filter → no violations."""
    assert _scan_files(["triage_with_armor.tf"]) == []


def test_knowledge_surfacer_template_alone_passes() -> None:
    """Template staged ahead of the RE is fine — no required-armor RE here."""
    assert _scan_files(["knowledge_surfacer_ok.tf"]) == []


def test_goal_steward_not_required_passes() -> None:
    """Goal Steward is NOT in PRD §4.4's required-armor list."""
    assert _scan_files(["goal_steward_no_armor.tf"]) == []


def test_comment_keyword_does_not_misclassify() -> None:
    """`triage` keyword in a comment of an unrelated resource must not
    flag — the resource is goal-steward, not triage."""
    assert _scan_files(["comment_with_triage_keyword.tf"]) == []


def test_no_terraform_dir_returns_empty(tmp_path: Path) -> None:
    """Pointing at a non-existent dir yields []. Mirrors the WS-A
    bootstrap state."""
    assert mac.find_violations(tmp_path / "does-not-exist") == []


# ---------------------- fail cases ----------------------


def test_triage_no_armor_fails() -> None:
    """Triage RE with no covering Template → 1 violation."""
    violations = _scan_files(["triage_no_armor.tf"])
    assert len(violations) == 1
    assert "asb-agent-triage" in violations[0]


def test_triage_disabled_armor_fails_with_filter_config_violation() -> None:
    """Triage Template with empty filter_config → fail with filter_config
    violation (no RE in this fixture, so only the filter_config issue)."""
    violations = _scan_files(["triage_disabled_armor.tf"])
    assert len(violations) == 1
    assert "filter_config" in violations[0]
    assert "asb-agent-triage" in violations[0]


def test_risk_watcher_variant_no_template_fails() -> None:
    """RE for asb-agent-risk-watcher-ecommerce, no Template → 1 violation."""
    violations = _scan_files(["risk_watcher_ecommerce_no_armor.tf"])
    assert len(violations) == 1
    assert "risk-watcher-ecommerce" in violations[0]


def test_mixed_resources_only_flags_offender() -> None:
    """mixed_resources has triage Template + RE (passes), risk-watcher RE
    without Template (fails), morning brief RE (not required-armor, passes).
    Expect exactly 1 violation: risk-watcher. (KS retired per ADR 0059.)"""
    violations = _scan_files(["mixed_resources.tf"])
    assert len(violations) == 1
    assert "risk-watcher" in violations[0]
    assert "morning-brief" not in violations[0]


# ---------------------- multi-fixture combinations ----------------------


def test_three_required_agents_each_with_template_all_pass() -> None:
    """Synthesize a fixture set where all three required-armor agents have
    matching Templates AND REs. No violations expected."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="asb-armor-all-"))
    (tmp / "triage.tf").write_text((FIXTURES / "triage_with_armor.tf").read_text())
    (tmp / "ks.tf").write_text((FIXTURES / "knowledge_surfacer_ok.tf").read_text())
    (tmp / "rw.tf").write_text(
        """
        resource "google_model_armor_template" "tb_agent_risk_watcher" {
          template_id = "asb-agent-risk-watcher"
          location    = "us-central1"
          parent      = "projects/agency-brain-demo/locations/us-central1"
          filter_config {
            pi_and_jailbreak_filter_settings {
              filter_enforcement = "ENABLED"
            }
          }
        }
        """
    )
    assert mac.find_violations(tmp) == []
