import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sa_allowlist_check.py"
spec = importlib.util.spec_from_file_location("sa_allowlist_check", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_no_sas_passes():
    assert module.find_violations([]) == []


def test_disabled_compute_default_passes():
    sas = [
        {
            "email": "000000000000-compute@developer.gserviceaccount.com",
            "disabled": True,
        }
    ]
    assert module.find_violations(sas) == []


def test_enabled_compute_default_fails():
    sas = [
        {
            "email": "000000000000-compute@developer.gserviceaccount.com",
            "disabled": False,
        }
    ]
    violations = module.find_violations(sas)
    assert len(violations) == 1
    assert "must be disabled" in violations[0]


def test_unallowlisted_sa_fails():
    sas = [
        {
            "email": "rogue@agency-brain-demo.iam.gserviceaccount.com",
            "disabled": False,
        }
    ]
    violations = module.find_violations(sas)
    assert len(violations) == 1
    assert "unallowlisted" in violations[0]


def test_real_prod_state_passes():
    # Mirror of `gcloud iam service-accounts list --project=agency-brain-demo`
    # at the time this PR landed. Compute default disabled, WS-B SAs allowlisted.
    sas = [
        {
            "email": "asb-airtable-sync-invoker@agency-brain-demo.iam.gserviceaccount.com",
            "disabled": False,
        },
        {
            "email": "asb-sync-airtable-sa@agency-brain-demo.iam.gserviceaccount.com",
            "disabled": False,
        },
        {
            "email": "000000000000-compute@developer.gserviceaccount.com",
            "disabled": True,
        },
    ]
    assert module.find_violations(sas) == []
