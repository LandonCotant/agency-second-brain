"""Solutions Shared Drive folder discovery — ADR 0048.

Two roots are swept:

1. ``05_CLIENTS/`` — discovery walks immediate children (one per client),
   filters out the ``00_CLIENT_TEMPLATE`` skeleton and any client whose
   normalized folder name matches a HIPAA-flagged account, then for each
   remaining client lists a fixed allowlist of subfolders. Each leaf
   subfolder becomes one ``FolderConfig`` with ``client_name`` populated.

2. ``01_MANAGEMENT & LEGAL`` / ``02_FINANCE & ACCOUNTING`` /
   ``03_OPERATIONS & HR`` / ``04_SALES & MARKETING (Internal)`` — no
   discovery step; the caller hands each root id directly as a recursive
   ``FolderConfig``. The drive client's BFS walker handles the descent.

The HIPAA exclusion is intentional defense-in-depth — by NOT listing
files inside HIPAA-flagged client folders, we never download or embed
HIPAA content, which avoids any risk of it leaking into ``/ask`` via
the corpus. See ADR 0048 §3 + the project memory ``project_hipaa_deferred.md``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Protocol

from .drive_client import FolderConfig
from .models import NoteFolder

log = logging.getLogger("agency_brain.agents.notes_ingestor.solutions_discovery")

# ADR 0048 §3 — allowlisted client subfolders. Subfolders outside this
# set are skipped (LEGAL_ADMIN, CLIENT_BRAND_LIBRARY, DELIVERABLES,
# AGENT_WORKSPACE) because they contain non-text content or material
# that is already canonical in Airtable.
CLIENT_SUBFOLDER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "00_ONBOARDING",
        "01_STRATEGY",
        "05_CAMPAIGNS_AND_CHANNELS",
        "07_REPORTING (EXTERNAL)",
        "08_MEETING_NOTES",
    }
)

# Folder names that are immediate children of 05_CLIENTS/ but should
# never be ingested — template skeletons, scratch areas, etc.
CLIENT_FOLDER_SKIP: frozenset[str] = frozenset({"00_CLIENT_TEMPLATE"})

_NORMALIZE_PREFIX_RE = re.compile(r"^\d+[_\s]+")
_NORMALIZE_DOUBLE_UNDERSCORE_RE = re.compile(r"_+")


def normalize_folder_name(name: str) -> str:
    """Normalize a client folder name for matching against
    ``airtable_replica.accounts.company_name``.

    Examples
    --------
    ``06_CLIENT_A`` → ``client a``
    ``05_CLIENT_C_STUDIO`` → ``client c studio``
    ``02_CLIENT_D__RAIN_CO`` → ``elijah rain ministries`` (double-underscore collapse)
    ``Client A`` → ``client a``

    The match is case-folded + whitespace-collapsed so that the Airtable
    account name doesn't need to use the same casing as the folder.
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = _NORMALIZE_PREFIX_RE.sub("", s)
    s = _NORMALIZE_DOUBLE_UNDERSCORE_RE.sub("_", s)
    s = s.replace("_", " ")
    s = " ".join(s.split())
    return s.casefold()


class BQQueryClient(Protocol):
    """Matches ``writer.BQQueryClient`` — parameterized SELECT helper."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


def load_hipaa_account_names(bq_query: BQQueryClient, *, project_id: str) -> frozenset[str]:
    """Read all HIPAA-flagged account names from ``airtable_replica.accounts``.

    Returns the set of normalized account names. An empty set is the
    correct value when no HIPAA accounts exist; callers should still
    apply the filter so this stays a constant-time check at scale.

    A query failure is logged and re-raised — the caller decides whether
    to skip Solutions client ingestion (safer) or proceed without the
    HIPAA filter (do NOT — that would risk leaking HIPAA content to /ask).
    """
    sql = (
        f"SELECT company_name FROM `{project_id}.airtable_replica.accounts` "  # noqa: S608
        "WHERE hipaa = TRUE"
    )
    rows = bq_query.query_rows(sql)
    out = {normalize_folder_name(r.get("company_name") or "") for r in rows}
    out.discard("")
    return frozenset(out)


class _DriveLister(Protocol):
    """Subset of ``NotesDriveClient`` used by the discovery step."""

    def list_immediate_subfolders(self, parent_id: str) -> list[dict[str, str]]: ...


def expand_solutions_clients(
    drive: _DriveLister,
    *,
    clients_root_id: str,
    hipaa_account_names: Iterable[str],
    clients_root_path: str = "05_CLIENTS",
) -> list[FolderConfig]:
    """Walk ``05_CLIENTS/`` and return one ``FolderConfig`` per
    (client x allowlisted-subfolder).

    Steps per ADR 0048 §3:

    1. List immediate child folders of ``clients_root_id``.
    2. Skip names in ``CLIENT_FOLDER_SKIP``.
    3. Skip clients whose normalized name is in ``hipaa_account_names``
       (logged at INFO so the operator can confirm the match).
    4. For each remaining client, list immediate subfolders and keep
       only those whose names match ``CLIENT_SUBFOLDER_ALLOWLIST``.
    5. Yield one recursive ``FolderConfig`` per matched subfolder, with
       ``client_name`` set to the normalized account name in title-case
       form (e.g., ``Client A``) and ``source_path`` set to a
       human-readable relative path.

    A non-matching client name (no Airtable account row) is NOT a HIPAA
    skip — it's logged at INFO and ingested. The first prod tick will
    surface any name drift and the operator can either rename the
    folder or add the account.
    """
    hipaa_set = set(hipaa_account_names)
    configs: list[FolderConfig] = []

    try:
        clients = drive.list_immediate_subfolders(clients_root_id)
    except Exception:
        log.exception(
            "solutions_discovery.list_clients_failed root_id=%s",
            clients_root_id,
        )
        return []

    for client in clients:
        client_folder_name = client["name"]
        client_folder_id = client["id"]
        if client_folder_name in CLIENT_FOLDER_SKIP:
            log.info(
                "solutions_discovery.skip_template name=%s",
                client_folder_name,
            )
            continue

        normalized = normalize_folder_name(client_folder_name)
        if normalized in hipaa_set:
            log.info(
                "solutions_discovery.hipaa_skip client_folder=%s normalized=%s",
                client_folder_name,
                normalized,
            )
            continue

        # Pretty client name for the Markdown header: "06_CLIENT_A"
        # → "Client A". If the normalize step stripped the name to
        # nothing, fall back to the raw folder name (which preserves
        # whatever the user wrote).
        client_display = normalized.title() if normalized else client_folder_name

        try:
            subfolders = drive.list_immediate_subfolders(client_folder_id)
        except Exception:
            log.exception(
                "solutions_discovery.list_subfolders_failed client_folder=%s",
                client_folder_name,
            )
            continue

        matched_any = False
        for sub in subfolders:
            sub_name = sub["name"]
            if sub_name not in CLIENT_SUBFOLDER_ALLOWLIST:
                continue
            matched_any = True
            source_path = f"{clients_root_path}/{client_folder_name}/{sub_name}"
            configs.append(
                FolderConfig(
                    folder_id=sub["id"],
                    role=NoteFolder.SOLUTIONS_CLIENT,
                    client_name=client_display,
                    source_path=source_path,
                    recursive=True,
                )
            )

        if not matched_any:
            log.info(
                "solutions_discovery.no_allowlisted_subfolders client=%s",
                client_folder_name,
            )

    log.info(
        "solutions_discovery.clients_expanded n_clients=%d n_leaf_configs=%d",
        len(clients),
        len(configs),
    )
    return configs


def internal_folder_config(*, folder_id: str, source_path: str) -> FolderConfig:
    """Build a recursive ``FolderConfig`` for a top-level Solutions
    internal folder (Management/Finance/Ops/Sales).

    No discovery — the BFS walker in ``drive_client._walk_subfolders``
    handles the descent at list time. ``client_name`` stays None;
    ``source_path`` is the folder's top-level name (e.g.,
    ``04_SALES & MARKETING (Internal)``).
    """
    return FolderConfig(
        folder_id=folder_id,
        role=NoteFolder.SOLUTIONS_INTERNAL,
        client_name=None,
        source_path=source_path,
        recursive=True,
    )


__all__ = [
    "CLIENT_SUBFOLDER_ALLOWLIST",
    "CLIENT_FOLDER_SKIP",
    "BQQueryClient",
    "expand_solutions_clients",
    "internal_folder_config",
    "load_hipaa_account_names",
    "normalize_folder_name",
]
