"""Shared GCP client setup for the MCP server.

Runs as a subprocess of an MCP client (Claude Desktop, etc.) on the
operator's Mac. Auth is via the operator's ADC
(`gcloud auth application-default login`), which has owner on the
brain project. Tool-level guards (status='drafted' checks, MERGE
keys, embedding hashes) are the safety wrappers — see ADR 0051 §2.

BQ + Vertex calls use operator ADC directly (cloud-platform scope
suffices). Drive + Docs calls impersonate `asb-agent-triage-sa` — the
ADC OAuth client has a hardcoded scope allowlist that drops `drive`
silently at consent, so user-cred ADC can't talk to Drive no matter
what scopes Python requests. Impersonation sidesteps this: the SA
self-mints a Drive-scoped token via `iamcredentials.generateAccessToken`,
which works because the operator (project owner) implicitly has
`iam.serviceAccounts.getAccessToken` on every SA. ADR 0044's
folder-share invariant is preserved — operator shares the rollup
folders with the SA's email, same as Evening Reflection does.

No service account key, no Cloud Run service hop, no `/api/ask` HTTP
round-trip. The retrieval pipeline (`Retriever`, `VertexEmbedder`) is
imported directly from the Knowledge Surfacer / Notes Ingestor
packages so we share one implementation of the embed + VECTOR_SEARCH
recipe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val or ""


@dataclass(frozen=True)
class Config:
    """Resolved MCP server configuration, frozen for the process lifetime."""

    project_id: str
    location: str
    notes_dataset: str
    notes_table: str
    outputs_dataset: str
    cosine_threshold: float
    top_k_default: int
    half_life_days: float
    hybrid_enabled: bool
    rrf_k: int
    commitment_stale_days: int
    links_table: str
    drive_impersonation_sa: str


@cache
def get_config() -> Config:
    """Resolve once per process, cache for the rest."""
    return Config(
        project_id=_env("BRAIN_PROJECT_ID", "agency-brain-demo"),
        location=_env("BRAIN_VERTEX_LOCATION", "us-central1"),
        notes_dataset=_env("BRAIN_NOTES_DATASET", "agent_outputs"),
        notes_table=_env("BRAIN_NOTES_TABLE", "notes"),
        outputs_dataset=_env("BRAIN_OUTPUTS_DATASET", "agent_outputs"),
        # Lower than the Knowledge Surfacer's 0.50 module-default
        # because the MCP path returns chunks to Claude (which can
        # filter further during synthesis), whereas the Surfacer's
        # `/api/ask` runs a synthesizer that needs higher precision.
        cosine_threshold=float(_env("BRAIN_MCP_COSINE_THRESHOLD", "0.50")),
        top_k_default=int(_env("BRAIN_MCP_TOP_K_DEFAULT", "8")),
        # Recency half-life in days for brain_ask reranking. A note N
        # days old is reweighted by EXP(-N/half_life). Set <= 0 to
        # disable (pure cosine ranking).
        half_life_days=float(_env("BRAIN_ASK_HALF_LIFE_DAYS", "30")),
        # ADR 0068 — hybrid retrieval (vector + keyword, RRF-fused) for
        # brain_ask. On by default; flip BRAIN_HYBRID_ENABLED=false to
        # revert to pure-vector instantly (no index teardown needed).
        # BRAIN_RRF_K is the Reciprocal Rank Fusion constant (canonical 60).
        hybrid_enabled=_env("BRAIN_HYBRID_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        rrf_k=int(_env("BRAIN_RRF_K", "60")),
        # ADR 0069 — a commitment with no explicit due_date goes "overdue"
        # this many days after extraction. Must match the extractor Job's
        # COMMITMENT_STALE_DAYS so open_commitments + Morning Brief agree.
        commitment_stale_days=int(_env("COMMITMENT_STALE_DAYS", "7")),
        # notes_links table for wikilink edge writes (ADR 0053) +
        # related_notes traversal.
        links_table=_env("BRAIN_LINKS_TABLE", "notes_links"),
        # SA the MCP server impersonates for Drive/Docs writes. Defaults
        # to asb-agent-triage-sa (already shared on the reflections folder
        # by Evening Reflection's setup). Operator must share the briefs
        # + reviews folders with this SA too — see mcp_server/README.md.
        drive_impersonation_sa=_env(
            "BRAIN_DRIVE_IMPERSONATION_SA",
            "asb-agent-triage-sa@agency-brain-demo.iam.gserviceaccount.com",
        ),
    )


@cache
def bq_client():
    """Lazy-import BigQuery client. Reused across all tool invocations."""
    from google.cloud import bigquery

    return bigquery.Client(project=get_config().project_id)


@cache
def embedder():
    """Lazy-import Vertex embedder. Reused across all retrieval calls."""
    from ..agents.notes_ingestor.embedder import VertexEmbedder

    cfg = get_config()
    return VertexEmbedder(project_id=cfg.project_id, location=cfg.location)


@cache
def _drive_scoped_creds():
    """Operator ADC → impersonated SA creds with the Drive scope.

    Shared by drive_client + docs_client so we only do the impersonation
    handshake once per process. Drive scope alone covers both Drive API
    (files.list/create/export) and Docs API (documents.batchUpdate) for
    Docs the SA can reach — see the Docs API auth matrix.
    """
    from google.auth import default, impersonated_credentials

    source_creds, _ = default()
    return impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=get_config().drive_impersonation_sa,
        target_scopes=["https://www.googleapis.com/auth/drive"],
        lifetime=3600,
    )


@cache
def drive_client():
    """Lazy-import Drive v3 client. Impersonates the configured SA."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_drive_scoped_creds(), cache_discovery=False)


@cache
def docs_client():
    """Lazy-import Docs v1 client. Shares creds with drive_client."""
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=_drive_scoped_creds(), cache_discovery=False)


def query_rows(sql: str, parameters: list[dict] | None = None) -> list[dict]:
    """Execute a parameterized BQ query and return rows as dicts.

    The MCP tools call this exclusively — no string interpolation of
    user input. Calendar-ingester-style adapter shape so we can swap
    in a fake for tests.
    """
    from google.cloud import bigquery

    params = []
    for p in parameters or []:
        ptype = p["type"]
        if ptype.startswith("ARRAY_"):
            inner = ptype.removeprefix("ARRAY_")
            params.append(bigquery.ArrayQueryParameter(p["name"], inner, p["value"]))
        else:
            params.append(bigquery.ScalarQueryParameter(p["name"], ptype, p["value"]))
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(r) for r in bq_client().query(sql, job_config=job_config).result()]
