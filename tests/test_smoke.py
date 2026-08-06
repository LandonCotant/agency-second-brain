"""Import-smoke test: every deployed entrypoint module must import cleanly.

Per the CLAUDE.md gotchas, per-agent Dockerfiles pin their own deps and a
missing dep surfaces as a runtime ModuleNotFoundError, not a build failure.
This test catches the *dev-env* half of that class (a module-level import
that no longer resolves); the Docker-image half still needs a smoke-fire.

The module list mirrors the Dockerfile CMD entrypoints — keep them in sync.
"""

import importlib

import pytest

# One entry per Dockerfile CMD (Dockerfile.audit has no CMD; its job specs
# invoke the audit modules listed individually below).
ENTRYPOINT_MODULES = [
    "agency_brain.sync.airtable_to_bq",
    "agency_brain.agents.brag_spotter.main",
    "agency_brain.agents.calendar_ingester.main",
    "agency_brain.agents.captures_materializer.main",
    "agency_brain.agents.crm_updater.main",
    "agency_brain.agents.evening_reflection.main",
    "agency_brain.agents.librarian.main",
    "agency_brain.agents.morning_brief.main",
    "agency_brain.agents.notes_ingestor.main",
    "agency_brain.agents.people_sync.main",
    "agency_brain.agents.risk_watcher.main",
    "agency_brain.agents.triage.bridge",
    "agency_brain.routing.fanout_main",
    # Audit image entrypoints (args-override per runtime_audits.tf).
    "agency_brain.audit.airtable_schema_drift",
    "agency_brain.audit.bucket_iam_drift",
    "agency_brain.audit.daily_spend_check",
    "agency_brain.audit.drafts_boundary_check",
    "agency_brain.audit.hipaa_iam_drift",
    "agency_brain.audit.hipaa_isolation_check",
]


@pytest.mark.parametrize("module_name", ENTRYPOINT_MODULES)
def test_entrypoint_imports(module_name: str) -> None:
    importlib.import_module(module_name)
