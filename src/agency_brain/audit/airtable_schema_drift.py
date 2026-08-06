"""Airtable schema drift runtime check (W3 hardening, daily).

Runs as Cloud Run Job ``asb-audit-airtable-schema-drift`` on a daily Cloud
Scheduler cadence (02:00 UTC). Detects two classes of drift between
``airtable/schema.json`` and the live Operations base:

1. **Metadata drift** — field names + types + table names from the
   Airtable Meta API vs. ``schema.json`` declarations. Catches: someone
   renamed a field, added a field, or changed a field type.
2. **Operational drift** — for every field declared ``required: true``,
   query Airtable for rows where the field is blank. Any returned row is
   the Phase 0 bug class (PRs #141 / #143 / #170 / etc.): schema.json's
   REQUIRED contract isn't held by the live data, and the next sync will
   400 with ``Only optional fields can be set to NULL``.

The pure-logic functions are also imported by the CLI script
``scripts/check_airtable_schema_drift.py`` so the manual-run and
scheduled-run paths share one source of truth (W3 plan).

Required env vars (set by Terraform in
``terraform/modules/security/runtime_audits.tf``):

- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL`` — populated by Terraform from the SA email
- ``AIRTABLE_BASE_ID`` — defaults to ``appXXXXXXXXXXXXXX`` (Operations base)
- ``AIRTABLE_PAT_SECRET_ID`` — Secret Manager secret holding the PAT
  (read-only on the base). Defaults to ``airtable-pat-prod``.
- Optional ``SCHEMA_JSON_PATH`` — defaults to the bundled
  ``/app/airtable/schema.json`` in the audit image (Cloud Run) or the
  repo-relative path when running from a checkout.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..common.audit_log import AuditLogClient
from ._reporter import AuditScriptContext, DriftReporter

log = logging.getLogger("agency_brain.audit.airtable_schema_drift")

DEFAULT_BASE_ID = "appXXXXXXXXXXXXXX"
DEFAULT_PAT_SECRET_ID = "airtable-pat-prod"
AIRTABLE_API_BASE = "https://api.airtable.com/v0"


@dataclass
class Drift:
    """One actionable drift finding.

    ``category`` is one of: ``missing_table``, ``new_table``,
    ``missing_field``, ``new_field``, ``type_change``, ``required_violation``.
    """

    category: str
    table: str
    field: str | None
    detail: str


# ---------------------------------------------------------------------------
# Pure-logic: declared-side normalization
# ---------------------------------------------------------------------------


def normalize_declared_schema(schema_json: dict) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{table: {field: {type, required}}}`` from ``schema.json``.

    Lookup fields (``multipleLookupValues``) are skipped — they're filtered
    out of BQ replication by ``schema_mapping.py`` and don't have a
    counterpart in the live base meta that's worth diffing here.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name, table_def in schema_json.get("tables", {}).items():
        fields: dict[str, dict[str, Any]] = {}
        for f in table_def.get("fields", []):
            ftype = f["type"]
            if ftype == "multipleLookupValues":
                continue
            fields[f["name"]] = {
                "type": ftype,
                "required": bool(f.get("required", False)),
            }
        out[table_name] = fields
    return out


# ---------------------------------------------------------------------------
# Pure-logic: live-side normalization
# ---------------------------------------------------------------------------


def normalize_live_schema(meta_response: dict) -> dict[str, dict[str, str]]:
    """Return ``{table: {field: type}}`` from the Airtable Meta API response.

    Lookup fields are filtered out to match the declared-side normalization —
    schema.json drops them via the multipleLookupValues skip, so live must
    drop them too to avoid spurious ``new_field`` reports.

    **Known false-positive class — auto-reverse-link mirrors.** Airtable
    auto-generates a ``multipleRecordLinks`` field on table B when table A
    has a forward link to B. The Meta API marks both sides identically
    (``options.isReversed = false`` on both, per 2026-05-28 audit) so the
    script cannot distinguish them by metadata alone. Cross-table inference
    is possible (skip a live link iff the linked table declares a link
    back) but adds complexity; the noise here is ~6 findings on the
    Operations base and operators can recognize the pattern at a glance.

    The Meta API does NOT expose a ``required`` flag on fields — required is
    enforced at the UI/form layer, not the schema layer. That's exactly why
    operational drift detection (below) is the load-bearing part of this
    check: REQUIRED is a property of the data, not the metadata.
    """
    out: dict[str, dict[str, str]] = {}
    for table in meta_response.get("tables", []):
        fields: dict[str, str] = {}
        for f in table.get("fields", []):
            if f["type"] == "multipleLookupValues":
                continue
            fields[f["name"]] = f["type"]
        out[table["name"]] = fields
    return out


# ---------------------------------------------------------------------------
# Pure-logic: metadata diff
# ---------------------------------------------------------------------------


def diff_metadata(
    declared: dict[str, dict[str, dict[str, Any]]],
    live: dict[str, dict[str, str]],
) -> list[Drift]:
    """Compare declared vs. live; return one Drift per finding."""
    drifts: list[Drift] = []

    declared_tables = set(declared.keys())
    live_tables = set(live.keys())

    for t in sorted(declared_tables - live_tables):
        drifts.append(
            Drift(
                category="missing_table",
                table=t,
                field=None,
                detail="declared in schema.json but absent from live base",
            )
        )
    for t in sorted(live_tables - declared_tables):
        drifts.append(
            Drift(
                category="new_table",
                table=t,
                field=None,
                detail="present in live base but not in schema.json",
            )
        )

    for t in sorted(declared_tables & live_tables):
        declared_fields = declared[t]
        live_fields = live[t]
        for f in sorted(set(declared_fields) - set(live_fields)):
            drifts.append(
                Drift(
                    category="missing_field",
                    table=t,
                    field=f,
                    detail=f"declared (type={declared_fields[f]['type']}) but absent in live",
                )
            )
        for f in sorted(set(live_fields) - set(declared_fields)):
            drifts.append(
                Drift(
                    category="new_field",
                    table=t,
                    field=f,
                    detail=f"in live base (type={live_fields[f]}) but not declared",
                )
            )
        for f in sorted(set(declared_fields) & set(live_fields)):
            d_type = declared_fields[f]["type"]
            l_type = live_fields[f]
            if d_type != l_type:
                drifts.append(
                    Drift(
                        category="type_change",
                        table=t,
                        field=f,
                        detail=f"declared={d_type} live={l_type}",
                    )
                )

    return drifts


# ---------------------------------------------------------------------------
# Pure-logic: operational drift
# ---------------------------------------------------------------------------


# Field types where a blank-in-Airtable row maps to a non-NULL BQ value
# (so operational drift would be a false positive). Checkboxes map to
# ``BOOL false``, NOT NULL — the sync writes ``false`` for unchecked rows.
_OPERATIONAL_DRIFT_SKIP_TYPES: frozenset[str] = frozenset({"checkbox"})


def required_fields_by_table(
    declared: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, list[str]]:
    """Return ``{table: [field, ...]}`` for every field declared required
    AND whose type would cause a NULL-on-REQUIRED sync failure if blank.

    Checkbox fields are skipped: an unchecked checkbox is BLANK in Airtable
    but maps to ``BOOL false`` in BigQuery via the sync, so a blank-checkbox
    REQUIRED field never crashes the sync. Pinning this avoids the false
    positive that flagged ``Accounts.HIPAA`` on the first live run of this
    check (2026-05-28).
    """
    out: dict[str, list[str]] = {}
    for t, fields in declared.items():
        names = [
            name
            for name, spec in fields.items()
            if spec["required"] and spec["type"] not in _OPERATIONAL_DRIFT_SKIP_TYPES
        ]
        if names:
            out[t] = sorted(names)
    return out


def build_blank_field_formula(field_name: str) -> str:
    """filterByFormula clause matching rows where ``field_name`` is blank.

    The Airtable formula language uses ``BLANK()`` and ``{Field Name}``
    syntax. For multi-value fields (multipleRecordLinks, multipleSelects),
    an empty array reads as blank via the same predicate.
    """
    safe_field = field_name.replace("'", "\\'")
    return f"AND({{{safe_field}}} = BLANK())"


def operational_drift(
    required: dict[str, list[str]],
    fetcher: Any,
) -> list[Drift]:
    """Run one Airtable query per required field; return drifts for any matches.

    ``fetcher`` is a callable ``(table_name, filter_formula) -> list[record]``.
    Decoupled so tests can inject canned responses. Production callers wire
    this to the Airtable REST API via ``make_required_field_fetcher``.
    """
    drifts: list[Drift] = []
    for table, fields in required.items():
        for field in fields:
            formula = build_blank_field_formula(field)
            records = fetcher(table, formula)
            if records:
                example_ids = [r.get("id", "?") for r in records[:3]]
                drifts.append(
                    Drift(
                        category="required_violation",
                        table=table,
                        field=field,
                        detail=(
                            f"{len(records)} live row(s) with blank '{field}'; "
                            f"sample rec ids: {', '.join(example_ids)}. "
                            f"Sync will 400 on next run."
                        ),
                    )
                )
    return drifts


# ---------------------------------------------------------------------------
# Pure-logic: reporting
# ---------------------------------------------------------------------------


def format_report(drifts: list[Drift]) -> str:
    if not drifts:
        return "✓ No drift detected. Schema.json matches the live Operations base."

    by_category: dict[str, list[Drift]] = {}
    for d in drifts:
        by_category.setdefault(d.category, []).append(d)

    lines = [f"⚠ {len(drifts)} drift finding(s):"]
    for category in (
        "required_violation",
        "missing_field",
        "missing_table",
        "type_change",
        "new_field",
        "new_table",
    ):
        items = by_category.get(category)
        if not items:
            continue
        lines.append("")
        lines.append(f"[{category}]  ({len(items)})")
        for d in items:
            loc = f"{d.table}.{d.field}" if d.field else d.table
            lines.append(f"  • {loc}: {d.detail}")
    return "\n".join(lines)


def drift_summary(drifts: list[Drift]) -> dict[str, Any]:
    """Compact JSON-safe summary for ``DriftReporter.drift()`` payloads.

    ``DriftReporter`` serializes the summary into ``output`` on the BQ row
    plus the structured stdout JSON. Keep the shape stable so dashboards
    + Chat notifications can read consistent keys.
    """
    return {
        "finding_count": len(drifts),
        "by_category": dict(Counter(d.category for d in drifts)),
        "samples": [
            {
                "category": d.category,
                "table": d.table,
                "field": d.field,
                "detail": d.detail,
            }
            for d in drifts[:10]
        ],
    }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _http_get_json(url: str, pat: str) -> dict[str, Any]:
    """GET ``url`` with the Airtable PAT, parse JSON.

    Uses ``requests`` for consistency with ``sync/airtable_client.py``.
    """
    import requests

    resp = requests.get(url, headers={"Authorization": f"Bearer {pat}"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Airtable HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def fetch_live_meta(*, base_id: str, pat: str) -> dict:
    """Call the Airtable Meta API to get the base schema."""
    url = f"{AIRTABLE_API_BASE}/meta/bases/{base_id}/tables"
    return _http_get_json(url, pat)


def make_required_field_fetcher(*, base_id: str, pat: str):
    """Return a fetcher closure for ``operational_drift()``.

    The closure issues
    ``GET /v0/{base}/{table}?filterByFormula=...&maxRecords=10`` so
    REQUIRED-violation checks don't pull the whole table.
    """

    def fetch(table_name: str, filter_formula: str) -> list[dict]:
        url = (
            f"{AIRTABLE_API_BASE}/{base_id}/{quote(table_name)}"
            f"?filterByFormula={quote(filter_formula)}&maxRecords=10"
        )
        body = _http_get_json(url, pat)
        return body.get("records", [])

    return fetch


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_schema_path(explicit: str | None = None) -> Path:
    """Return the path to ``airtable/schema.json``.

    Order of precedence:

    1. ``explicit`` argument (passed by callers that already know the path).
    2. ``SCHEMA_JSON_PATH`` env var.
    3. ``/app/airtable/schema.json`` — the location ``Dockerfile.audit``
       copies the file to inside the Cloud Run image.
    4. Repo-relative path computed from ``__file__`` — useful when running
       the CLI script or tests from a checkout.

    The function does NOT verify the path exists; the caller's
    ``json.loads(path.read_text())`` will surface a clear error.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("SCHEMA_JSON_PATH")
    if env:
        return Path(env)
    in_container = Path("/app/airtable/schema.json")
    if in_container.exists():
        return in_container
    # Repo-relative fallback. Module path is src/agency_brain/audit/...
    # → parents[3] is the repo root.
    return Path(__file__).resolve().parents[3] / "airtable" / "schema.json"


def load_pat_from_secret(*, project_id: str, secret_id: str) -> str:
    """Read the Airtable PAT from Secret Manager.

    Mirrors the ``daily_spend_check.load_webhook_url`` pattern so audit
    Jobs use one consistent secret-access surface.
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


# ---------------------------------------------------------------------------
# Cloud Run Job entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-airtbl-drift@{project_id}.iam.gserviceaccount.com",
    )
    base_id = os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE_ID)
    pat_secret_id = os.environ.get("AIRTABLE_PAT_SECRET_ID", DEFAULT_PAT_SECRET_ID)

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="airtable-schema-drift",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    try:
        pat = load_pat_from_secret(project_id=project_id, secret_id=pat_secret_id)
        schema_path = resolve_schema_path()
        schema_json = json.loads(schema_path.read_text())
        declared = normalize_declared_schema(schema_json)
        meta = fetch_live_meta(base_id=base_id, pat=pat)
        live = normalize_live_schema(meta)
        drifts = diff_metadata(declared, live)
        fetcher = make_required_field_fetcher(base_id=base_id, pat=pat)
        drifts.extend(operational_drift(required_fields_by_table(declared), fetcher))
    except Exception as exc:  # — surface as audit error
        log.exception("airtable_schema_drift failed before completion")
        reporter.error(exc)
        return 1

    summary = drift_summary(drifts)
    summary["base_id"] = base_id

    if not drifts:
        reporter.ok(summary)
        return 0

    reporter.drift(summary)
    # Exit non-zero so the Cloud Run Job execution is flagged in the console
    # alongside the structured audit event. The audit row + log line are the
    # canonical signals; exit code is for operator visibility.
    return 2


if __name__ == "__main__":
    sys.exit(main())
