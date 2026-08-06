"""Unit tests for the Solutions Drive discovery step (ADR 0048).

Covers the three load-bearing behaviors:

1. Folder-name normalization for HIPAA matching (handles prefix
   stripping, underscore collapse, casefolding).
2. ``00_CLIENT_TEMPLATE`` skip + HIPAA folder skip.
3. Subfolder allowlist + ``FolderConfig`` shape (client_name,
   source_path, recursive=True).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agency_brain.agents.notes_ingestor.models import NoteFolder
from agency_brain.agents.notes_ingestor.solutions_discovery import (
    CLIENT_FOLDER_SKIP,
    CLIENT_SUBFOLDER_ALLOWLIST,
    expand_solutions_clients,
    internal_folder_config,
    load_hipaa_account_names,
    normalize_folder_name,
)

# ---------------------------------------------------------------------------
# normalize_folder_name
# ---------------------------------------------------------------------------


def test_normalize_strips_numeric_prefix_and_underscore_split() -> None:
    assert normalize_folder_name("06_CLIENT_A") == "client a"
    assert normalize_folder_name("05_CLIENT_C_STUDIO") == "client c studio"


def test_normalize_collapses_double_underscores() -> None:
    # The user's real-world tree has `02_CLIENT_D__RAIN_CO` with
    # a double underscore between first/last; the normalize step must
    # collapse runs of underscores so this matches a single-underscored
    # Airtable account name.
    assert normalize_folder_name("02_CLIENT_D__RAIN_CO") == "client d rain co"


def test_normalize_handles_plain_account_name() -> None:
    assert normalize_folder_name("Client A") == "client a"
    assert normalize_folder_name("  Client   A  ") == "client a"


def test_normalize_empty_safe() -> None:
    assert normalize_folder_name("") == ""
    assert normalize_folder_name(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_hipaa_account_names
# ---------------------------------------------------------------------------


@dataclass
class _FakeBQ:
    rows_by_sql: dict[str, list[dict]] = field(default_factory=dict)
    captured: list[tuple[str, list[dict] | None]] = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.captured.append((sql, parameters))
        return self.rows_by_sql.get(sql, [])


def test_load_hipaa_returns_normalized_set() -> None:
    sql = "SELECT company_name FROM `proj.airtable_replica.accounts` " "WHERE hipaa = TRUE"
    bq = _FakeBQ(
        rows_by_sql={
            sql: [
                {"company_name": "Acme Health"},
                {"company_name": "Ridge Therapy"},
            ]
        }
    )

    out = load_hipaa_account_names(bq, project_id="proj")

    assert out == frozenset({"acme health", "ridge therapy"})


def test_load_hipaa_drops_empty_account_names() -> None:
    sql = "SELECT company_name FROM `proj.airtable_replica.accounts` " "WHERE hipaa = TRUE"
    bq = _FakeBQ(
        rows_by_sql={
            sql: [
                {"company_name": "Acme"},
                {"company_name": ""},
                {"company_name": None},
            ]
        }
    )

    out = load_hipaa_account_names(bq, project_id="proj")

    assert out == frozenset({"acme"})


# ---------------------------------------------------------------------------
# expand_solutions_clients
# ---------------------------------------------------------------------------


@dataclass
class _FakeDrive:
    subfolders: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    """Maps parent_id -> immediate-child folders [{id, name}, ...]."""
    fail_on: set[str] = field(default_factory=set)
    """parent_ids that should raise on list_immediate_subfolders."""

    def list_immediate_subfolders(self, parent_id: str) -> list[dict[str, str]]:
        if parent_id in self.fail_on:
            raise RuntimeError(f"forced failure on {parent_id}")
        return self.subfolders.get(parent_id, [])


def test_expand_clients_skips_template_folder() -> None:
    drive = _FakeDrive(
        subfolders={
            "clients-root": [
                {"id": "tmpl-id", "name": "00_CLIENT_TEMPLATE"},
                {"id": "clienta-id", "name": "06_CLIENT_A"},
            ],
            "clienta-id": [
                {"id": "clienta-meeting-id", "name": "08_MEETING_NOTES"},
            ],
        }
    )

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset(),
    )

    folder_ids = [c.folder_id for c in configs]
    assert "clienta-meeting-id" in folder_ids
    # The template's children would never be listed (drive.fail would
    # have raised) but we also verify nothing for the template id leaked.
    assert "tmpl-id" not in folder_ids


def test_expand_clients_skips_hipaa_match() -> None:
    drive = _FakeDrive(
        subfolders={
            "clients-root": [
                {"id": "acme-id", "name": "07_ACME_HEALTH"},
                {"id": "clienta-id", "name": "06_CLIENT_A"},
            ],
            "acme-id": [
                {"id": "acme-meeting-id", "name": "08_MEETING_NOTES"},
            ],
            "clienta-id": [
                {"id": "clienta-meeting-id", "name": "08_MEETING_NOTES"},
            ],
        }
    )

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset({"acme health"}),
    )

    folder_ids = [c.folder_id for c in configs]
    # ACME (HIPAA) is entirely absent — never listed, never ingested.
    assert "acme-meeting-id" not in folder_ids
    # ClientA (non-HIPAA) is present.
    assert "clienta-meeting-id" in folder_ids


def test_expand_clients_filters_subfolder_allowlist() -> None:
    drive = _FakeDrive(
        subfolders={
            "clients-root": [
                {"id": "clienta-id", "name": "06_CLIENT_A"},
            ],
            "clienta-id": [
                {"id": "onboarding", "name": "00_ONBOARDING"},  # allowlisted
                {"id": "strategy", "name": "01_STRATEGY"},  # allowlisted
                {"id": "legal", "name": "02_LEGAL_ADMIN"},  # SKIPPED
                {"id": "brand", "name": "03_CLIENT_BRAND_LIBRARY"},  # SKIPPED
                {"id": "campaigns", "name": "05_CAMPAIGNS_AND_CHANNELS"},  # allowlisted
                {"id": "deliverables", "name": "06_DELIVERABLES (Final Versions ...)"},  # SKIPPED
                {"id": "reporting", "name": "07_REPORTING (EXTERNAL)"},  # allowlisted
                {"id": "meetings", "name": "08_MEETING_NOTES"},  # allowlisted
                {"id": "agent_ws", "name": "09_AGENT_WORKSPACE"},  # SKIPPED
            ],
        }
    )

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset(),
    )

    matched_ids = {c.folder_id for c in configs}
    assert matched_ids == {"onboarding", "strategy", "campaigns", "reporting", "meetings"}


def test_expand_clients_populates_client_name_and_source_path() -> None:
    drive = _FakeDrive(
        subfolders={
            "clients-root": [
                {"id": "clienta-id", "name": "06_CLIENT_A"},
            ],
            "clienta-id": [
                {"id": "meetings", "name": "08_MEETING_NOTES"},
            ],
        }
    )

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset(),
    )

    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.role is NoteFolder.SOLUTIONS_CLIENT
    assert cfg.client_name == "Client A"
    assert cfg.source_path == "05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES"
    assert cfg.recursive is True


def test_expand_clients_survives_subfolder_list_failure() -> None:
    """One client failing to list shouldn't take down the rest."""
    drive = _FakeDrive(
        subfolders={
            "clients-root": [
                {"id": "broken-id", "name": "99_BROKEN"},
                {"id": "clienta-id", "name": "06_CLIENT_A"},
            ],
            "clienta-id": [
                {"id": "meetings", "name": "08_MEETING_NOTES"},
            ],
        },
        fail_on={"broken-id"},
    )

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset(),
    )

    assert [c.folder_id for c in configs] == ["meetings"]


def test_expand_clients_returns_empty_on_root_failure() -> None:
    drive = _FakeDrive(fail_on={"clients-root"})

    configs = expand_solutions_clients(
        drive,
        clients_root_id="clients-root",
        hipaa_account_names=frozenset(),
    )

    assert configs == []


# ---------------------------------------------------------------------------
# internal_folder_config
# ---------------------------------------------------------------------------


def test_internal_folder_config_is_recursive_agency_area() -> None:
    cfg = internal_folder_config(
        folder_id="sales-id",
        source_path="04_SALES & MARKETING (Internal)",
    )

    assert cfg.folder_id == "sales-id"
    assert cfg.role is NoteFolder.SOLUTIONS_INTERNAL
    assert cfg.client_name is None
    assert cfg.source_path == "04_SALES & MARKETING (Internal)"
    assert cfg.recursive is True


# ---------------------------------------------------------------------------
# Allowlist + skip-set sanity
# ---------------------------------------------------------------------------


def test_allowlist_constants_match_user_spec() -> None:
    """Source of truth for ADR 0048 §3 — keep this aligned with the ADR."""
    assert CLIENT_SUBFOLDER_ALLOWLIST == frozenset(
        {
            "00_ONBOARDING",
            "01_STRATEGY",
            "05_CAMPAIGNS_AND_CHANNELS",
            "07_REPORTING (EXTERNAL)",
            "08_MEETING_NOTES",
        }
    )
    assert CLIENT_FOLDER_SKIP == frozenset({"00_CLIENT_TEMPLATE"})
