"""CLI wrapper around ``agency_brain.audit.airtable_schema_drift``.

For manual operator use:

    AIRTABLE_PAT=$(gcloud secrets versions access latest --secret=airtable-pat-prod \\
                   --project=agency-brain-demo) \\
        python scripts/check_airtable_schema_drift.py

The Cloud Run Job ``asb-audit-airtable-schema-drift`` (W3 of the post-audit
hardening plan) imports the same pure-logic functions from
``src/agency_brain/audit/airtable_schema_drift.py`` so both paths use a
single source of truth.

The CLI's exit codes match the audit Job's:
- ``0`` — no drift detected.
- ``1`` — drift detected (printed to stdout in human-readable form).
- ``2`` — script error (missing env var, API failure, malformed schema.json).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# scripts/ isn't on the package path by default; add src/ so the import
# resolves when running this from a checkout (the cloudbuild image bundles
# src/ on PYTHONPATH so it works there too).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agency_brain.audit.airtable_schema_drift import (  # noqa: E402
    DEFAULT_BASE_ID,
    diff_metadata,
    fetch_live_meta,
    format_report,
    make_required_field_fetcher,
    normalize_declared_schema,
    normalize_live_schema,
    operational_drift,
    required_fields_by_table,
    resolve_schema_path,
)


def main(argv: list[str] | None = None) -> int:
    pat = os.environ.get("AIRTABLE_PAT")
    if not pat:
        sys.stderr.write(
            "ERROR: AIRTABLE_PAT env var is required. Try:\n"
            "  AIRTABLE_PAT=$(gcloud secrets versions access latest \\\n"
            "    --secret=airtable-pat-prod --project=agency-brain-demo)\n"
        )
        return 2

    base_id = os.environ.get("AIRTABLE_BASE_ID", DEFAULT_BASE_ID)
    schema_path = resolve_schema_path()

    try:
        schema_json = json.loads(schema_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"ERROR reading {schema_path}: {exc}\n")
        return 2

    declared = normalize_declared_schema(schema_json)

    try:
        meta = fetch_live_meta(base_id=base_id, pat=pat)
    except Exception as exc:  # — surface anything as a script error
        sys.stderr.write(f"ERROR fetching Meta API: {exc}\n")
        return 2
    live = normalize_live_schema(meta)

    drifts = diff_metadata(declared, live)

    fetcher = make_required_field_fetcher(base_id=base_id, pat=pat)
    required = required_fields_by_table(declared)
    try:
        drifts.extend(operational_drift(required, fetcher))
    except Exception as exc:
        sys.stderr.write(f"ERROR during operational drift checks: {exc}\n")
        return 2

    print(format_report(drifts))
    return 0 if not drifts else 1


if __name__ == "__main__":
    sys.exit(main())
