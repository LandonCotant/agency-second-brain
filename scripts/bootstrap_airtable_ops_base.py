#!/usr/bin/env python3
"""Bootstrap the Agency Operations Airtable base from airtable/schema.json.

One-shot creation of the Operations base described in ADR 0011.

Three-pass field creation because Airtable's Meta API enforces dependency order:
- **Pass 1:** create base + tables with scalar/select fields only.
- **Pass 2:** add multipleRecordLinks fields (need target tables to exist).
- **Pass 3:** add multipleLookupValues + count fields (need link fields to exist).

Then seed Service Catalog, Risk Profiles, and Team rows.

Idempotency: aborts if a base named "Agency Operations" already exists in
the workspace. To re-run, delete the existing base in Airtable first.

Usage:
    export AIRTABLE_BOOTSTRAP_PAT='pat...'   # Workspace-Owner scope; revoke after
    python3 scripts/bootstrap_airtable_ops_base.py

Env overrides (optional):
    AIRTABLE_WORKSPACE_ID  default: wspwsVbCT14kqrjyk
    AIRTABLE_BASE_NAME     default: "Agency Operations"
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "airtable" / "schema.json"

API_ROOT = "https://api.airtable.com/v0"
META_ROOT = f"{API_ROOT}/meta"

WORKSPACE_ID = os.environ.get("AIRTABLE_WORKSPACE_ID", "wspwsVbCT14kqrjyk")
BASE_NAME = os.environ.get("AIRTABLE_BASE_NAME", "Agency Operations")

# ---- Field-type translation -------------------------------------------------

# Field types we create directly in Pass 1 (scalar/select/collaborator/etc).
SCALAR_TYPES = {
    "singleLineText",
    "multilineText",
    "email",
    "url",
    "phoneNumber",
    "checkbox",
    "date",
    "number",
    "percent",
    "currency",
    "singleSelect",
    "multipleSelects",
    "singleCollaborator",
    "multipleCollaborators",
    "multipleAttachments",
}

# Field types that cannot be created via the Meta API (Airtable manages these
# internally — `createdTime` and `lastModifiedTime` are derivable from
# Airtable's per-record metadata `createdTime` field which is always present;
# `count` and `rollup` are derived from linked records but Airtable hasn't
# exposed creating them via API yet).
# We silently skip them at base creation; the user can add them manually in
# the UI in seconds. A summary of skipped fields is printed at the end.
SKIPPED_TYPES = {"createdTime", "lastModifiedTime", "formula", "rollup", "count"}
SKIPPED_FIELDS_LOG: list[str] = []

LINK_TYPE = "multipleRecordLinks"
LOOKUP_TYPE = "multipleLookupValues"
COUNT_TYPE = "count"


def _trim_desc(notes: str) -> str:
    """Airtable caps field/table descriptions; keep them under the limit."""
    if not notes:
        return ""
    return notes[:500]


def field_to_api_pass1(f: dict[str, Any], table_name: str = "") -> dict[str, Any] | None:
    """Translate a schema.json field into the Pass-1 Meta API field shape.

    Returns None for link/lookup/count types (handled in later passes) and
    for field types Airtable manages internally (createdTime/lastModifiedTime
    cannot be created via the Meta API).
    """
    t = f["type"]
    if t in {LINK_TYPE, LOOKUP_TYPE}:
        return None
    if t in SKIPPED_TYPES:
        if table_name:
            SKIPPED_FIELDS_LOG.append(f"  {table_name}.{f['name']:<22} ({t} — add manually)")
        return None
    if t not in SCALAR_TYPES:
        # Defensive: surface unknown types loudly so we add a mapping rather
        # than silently dropping fields.
        raise ValueError(f"Unsupported field type for pass-1 creation: {t!r} (field {f['name']!r})")

    out: dict[str, Any] = {"name": f["name"], "type": t}
    desc = _trim_desc(f.get("notes", ""))
    if desc:
        out["description"] = desc

    if t == "singleSelect" or t == "multipleSelects":
        opts = f.get("options", []) or []
        out["options"] = {"choices": [{"name": str(o)} for o in opts]}
    elif t == "checkbox":
        out["options"] = {"color": "greenBright", "icon": "check"}
    elif t == "date":
        out["options"] = {"dateFormat": {"name": "iso"}}
    elif t == "number":
        out["options"] = {"precision": 0}
    elif t == "percent":
        out["options"] = {"precision": 0}
    elif t == "currency":
        out["options"] = {"precision": 2, "symbol": "$"}
    return out


# ---- HTTP --------------------------------------------------------------------


def _pat() -> str:
    pat = os.environ.get("AIRTABLE_BOOTSTRAP_PAT")
    if not pat:
        sys.exit("AIRTABLE_BOOTSTRAP_PAT env var not set; aborting.")
    return pat


def _request(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {_pat()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_str = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} on {method} {url}\n{body_str}") from None


# ---- Bootstrap orchestration -------------------------------------------------


# Hardcoded creation order. Tables with no inbound links go first; Goals comes
# before Projects/Tasks/Goal Scores; Tasks last (depends on Projects).
TABLE_ORDER = [
    "Service Catalog",
    "Team",
    "Risk Profiles",
    "Clients",
    "Goals",
    "Projects",
    "Tasks",
    "Goal Scores",
]


def _is_reverse_link(f: dict[str, Any]) -> bool:
    """A reverse-link field is auto-created by Airtable when the forward link
    is created on the other table. We track these in schema.json for human
    readability + later renaming, but skip them in Pass 2 creation."""
    return f["type"] == LINK_TYPE and "Reverse link populated by" in f.get("notes", "")


def _forward_links(table_def: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in table_def["fields"] if f["type"] == LINK_TYPE and not _is_reverse_link(f)]


def _reverse_links(table_def: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in table_def["fields"] if _is_reverse_link(f)]


def abort_if_base_exists() -> None:
    resp = _request("GET", f"{META_ROOT}/bases")
    for base in resp.get("bases", []):
        if base["name"] == BASE_NAME:
            sys.exit(
                f"Base named {BASE_NAME!r} already exists (id={base['id']}). "
                f"Delete it in Airtable first or change AIRTABLE_BASE_NAME."
            )


def create_base_with_first_table(
    schema: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    """Create the base + the first table. Returns (base_id, table_ids, field_ids)."""
    first_name = TABLE_ORDER[0]
    tdef = schema["tables"][first_name]

    fields = []
    for f in tdef["fields"]:
        api_field = field_to_api_pass1(f, first_name)
        if api_field is not None:
            fields.append(api_field)

    body = {
        "name": BASE_NAME,
        "workspaceId": WORKSPACE_ID,
        "tables": [
            {
                "name": first_name,
                "description": _trim_desc(tdef.get("description", "")),
                "fields": fields,
            }
        ],
    }
    print(f"==> Creating base {BASE_NAME!r} in workspace {WORKSPACE_ID}")
    resp = _request("POST", f"{META_ROOT}/bases", body)
    base_id = resp["id"]
    table = resp["tables"][0]
    print(f"    base_id={base_id}  first_table={first_name} ({table['id']})")

    table_ids = {first_name: table["id"]}
    field_ids = {first_name: {f["name"]: f["id"] for f in table["fields"]}}
    return base_id, table_ids, field_ids


def create_remaining_tables(
    base_id: str,
    schema: dict[str, Any],
    table_ids: dict[str, str],
    field_ids: dict[str, dict[str, str]],
) -> None:
    print("==> Creating remaining tables")
    for tname in TABLE_ORDER[1:]:
        tdef = schema["tables"][tname]
        fields = []
        for f in tdef["fields"]:
            api_field = field_to_api_pass1(f, tname)
            if api_field is not None:
                fields.append(api_field)
        body = {
            "name": tname,
            "description": _trim_desc(tdef.get("description", "")),
            "fields": fields,
        }
        resp = _request("POST", f"{META_ROOT}/bases/{base_id}/tables", body)
        table_ids[tname] = resp["id"]
        field_ids[tname] = {f["name"]: f["id"] for f in resp["fields"]}
        print(f"    {tname:<18} {resp['id']}  ({len(resp['fields'])} fields)")
        time.sleep(0.25)  # be gentle with the meta API rate limit


def add_link_fields(
    base_id: str,
    schema: dict[str, Any],
    table_ids: dict[str, str],
    field_ids: dict[str, dict[str, str]],
) -> None:
    """Pass 2: add forward multipleRecordLinks fields. Airtable auto-creates
    reverse links on the target table; we capture those and (if the schema
    spec gives them a custom name) rename via PATCH."""
    print("==> Pass 2: link fields")
    # Build a lookup: target table → list of (source_table, source_field_name)
    # so we can rename auto-created reverses to match schema.json names.
    desired_reverses: dict[tuple[str, str], str] = {}
    # key: (target_table, source_table_name) → desired reverse field name on target_table
    for tname, tdef in schema["tables"].items():
        for f in _reverse_links(tdef):
            # The reverse has linked_table = the table that holds the forward link.
            # The notes say "Reverse link populated by {ForwardTable}.{ForwardFieldName}".
            note = f.get("notes", "")
            # We only need: the auto-created reverse on `tname` from a forward link
            # on `f['linked_table']` should be renamed to f['name'].
            desired_reverses[(tname, f["linked_table"])] = f["name"]

    for tname in TABLE_ORDER:
        tdef = schema["tables"][tname]
        for f in _forward_links(tdef):
            target = f["linked_table"]
            target_id = table_ids[target]
            body: dict[str, Any] = {
                "name": f["name"],
                "type": LINK_TYPE,
                "options": {"linkedTableId": target_id},
            }
            desc = _trim_desc(f.get("notes", ""))
            if desc:
                body["description"] = desc
            resp = _request(
                "POST", f"{META_ROOT}/bases/{base_id}/tables/{table_ids[tname]}/fields", body
            )
            field_ids[tname][f["name"]] = resp["id"]
            print(f"    {tname}.{f['name']:<22} → {target} ({resp['id']})")

            # Capture auto-created reverse field ID on the target table.
            inverse_id = (resp.get("options") or {}).get("inverseLinkFieldId")
            if inverse_id:
                # If the schema specifies a custom name for this reverse, rename it.
                desired_name = desired_reverses.get((target, tname))
                # Re-fetch target table to learn the current auto-name.
                target_schema = _request("GET", f"{META_ROOT}/bases/{base_id}/tables")
                for t in target_schema["tables"]:
                    if t["id"] == target_id:
                        for tf in t["fields"]:
                            if tf["id"] == inverse_id:
                                # Track the auto-created inverse so we can refer
                                # to it later (e.g., for `count` fields).
                                field_ids[target][tf["name"]] = inverse_id
                                if desired_name and tf["name"] != desired_name:
                                    # Rename it.
                                    rename_body = {"name": desired_name}
                                    _request(
                                        "PATCH",
                                        f"{META_ROOT}/bases/{base_id}/tables/{target_id}/fields/{inverse_id}",
                                        rename_body,
                                    )
                                    # Replace tracking under the new name.
                                    field_ids[target].pop(tf["name"], None)
                                    field_ids[target][desired_name] = inverse_id
                                    print(f"      reverse on {target} renamed to {desired_name!r}")
                                else:
                                    print(f"      reverse on {target} kept as {tf['name']!r}")
                                break
                        break
            time.sleep(0.25)


def add_lookup_and_count_fields(
    base_id: str,
    schema: dict[str, Any],
    table_ids: dict[str, str],
    field_ids: dict[str, dict[str, str]],
) -> None:
    """Pass 3: log lookup + count fields for manual addition.

    Airtable's Meta API doesn't currently support creating multipleLookupValues
    or count fields. We surface them in the final summary so the user adds
    them in the UI (~30 seconds per field, ~5 fields total)."""
    print("==> Pass 3: lookups + counts (Meta API doesn't create these — logging for manual)")
    for tname in TABLE_ORDER:
        tdef = schema["tables"][tname]
        for f in tdef["fields"]:
            t = f["type"]
            if t == LOOKUP_TYPE:
                link_field_name = f.get("lookup_from_link", "?")
                target_field_name = f.get("lookup_field", "?")
                # Find the linked table for human-readable description.
                link_field_def = next(
                    (
                        ff
                        for ff in tdef["fields"]
                        if ff["name"] == link_field_name
                        and ff["type"] == LINK_TYPE
                        and not _is_reverse_link(ff)
                    ),
                    None,
                )
                link_target_table = link_field_def["linked_table"] if link_field_def else "?"
                SKIPPED_FIELDS_LOG.append(
                    f"  {tname}.{f['name']:<22}  Lookup of {link_target_table}.{target_field_name} via {link_field_name} link"
                )
                print(f"    SKIP    {tname}.{f['name']:<22} (lookup — add manually)")
            elif t == COUNT_TYPE:
                link_field_name = f.get("rollup_from_link") or f.get("lookup_from_link") or "?"
                SKIPPED_FIELDS_LOG.append(
                    f"  {tname}.{f['name']:<22}  Count over {link_field_name} link"
                )
                print(f"    SKIP    {tname}.{f['name']:<22} (count — add manually)")


# ---- Seed data ---------------------------------------------------------------

SERVICE_CATALOG_SEEDS = [
    {
        "Service Name": "Vantage Local Platform",
        "Service Code": "vantage-local",
        "Category": "Platform Subscription",
        "Description": "SaaS that analyzes a client's CRM and sales data and drafts personalized campaigns using Vertex AI. Local-tier — for single-location and small-business clients. Self-serve with email support.",
        "Typical Engagement Length": "Ongoing",
        "Default Phase Sequence": "Onboarding → Active → Renewal Window",
        "Default Deliverables": "Platform access · Onboarding session · Quarterly check-in",
        "Standard Pricing Notes": "Monthly subscription. Pricing per Sales/CRM.",
        "Active": True,
        "Notes": "Platform Subscription category — no operational Project tracking required day-to-day. Onboarding may warrant a one-time Project at activation.",
    },
    {
        "Service Name": "Vantage Enterprise Platform",
        "Service Code": "vantage-enterprise",
        "Category": "Platform Subscription",
        "Description": "SaaS that analyzes a client's CRM and sales data and drafts personalized campaigns using Vertex AI. Enterprise-tier — for multi-location and larger clients. Includes white-glove onboarding and a dedicated CSM.",
        "Typical Engagement Length": "Ongoing",
        "Default Phase Sequence": "Onboarding → Active → Quarterly Business Review → Renewal Window",
        "Default Deliverables": "Platform access · White-glove onboarding · QBRs · Dedicated CSM contact",
        "Standard Pricing Notes": "Annual contract. Pricing per Sales/CRM.",
        "Active": True,
        "Notes": "Enterprise typically warrants a recurring Project to track the QBR cadence and CSM check-ins.",
    },
    {
        "Service Name": "Local SEO Lead Gen",
        "Service Code": "local-seo-leadgen",
        "Category": "Retainer",
        "Description": "Local search optimization and lead generation for service-area businesses. Ongoing keyword + GBP optimization, citation building, content production, lead tracking.",
        "Typical Engagement Length": "6-12 months",
        "Default Phase Sequence": "Discovery → Build → Launch → Maintain",
        "Default Deliverables": "Keyword strategy · GBP optimization · Monthly content · Citation building · Monthly reporting",
        "Standard Pricing Notes": "Monthly retainer.",
        "Active": True,
        "Notes": "Retainer category — one rolling Project per month per client.",
    },
    {
        "Service Name": "Website Design",
        "Service Code": "website-design",
        "Category": "Project-Based",
        "Description": "Custom website design and build. Includes discovery, wireframes, visual design, development, and launch.",
        "Typical Engagement Length": "3-6 months",
        "Default Phase Sequence": "Discovery → Design → Build → Launch → Closeout",
        "Default Deliverables": "Discovery brief · Wireframes · Visual designs · Built site · Launch checklist · Post-launch handoff",
        "Standard Pricing Notes": "Fixed-fee project.",
        "Active": True,
        "Notes": "Project-Based — one Project per Contract.",
    },
    {
        "Service Name": "Paid Ads Setup",
        "Service Code": "paid-ads-setup",
        "Category": "Project-Based",
        "Description": "Initial paid-ads campaign architecture and launch. Account structure, conversion tracking, audience segmentation, ad creative, launch QA. Hand-off to Paid Ads Management retainer post-launch.",
        "Typical Engagement Length": "1-3 months",
        "Default Phase Sequence": "Discovery → Build → Launch → Closeout",
        "Default Deliverables": "Account structure · Conversion tracking setup · Audience segments · Initial ad creatives · Launch report",
        "Standard Pricing Notes": "Fixed-fee project. Paired with Paid Ads Management retainer for ongoing optimization.",
        "Active": True,
        "Notes": "Project-Based — typically followed by a Paid Ads Management contract.",
    },
    {
        "Service Name": "Paid Ads Management",
        "Service Code": "paid-ads-mgmt",
        "Category": "Retainer",
        "Description": "Ongoing paid-ads optimization, A/B testing, budget management, monthly reporting. Continues from Paid Ads Setup.",
        "Typical Engagement Length": "Ongoing",
        "Default Phase Sequence": "Maintain (rolling)",
        "Default Deliverables": "Weekly optimization · Monthly performance report · Quarterly strategy review",
        "Standard Pricing Notes": "Monthly retainer.",
        "Active": True,
        "Notes": "Retainer — one rolling Project per month.",
    },
    {
        "Service Name": "Marketing Consulting Retainer",
        "Service Code": "marketing-consulting",
        "Category": "Retainer",
        "Description": "Strategic marketing consulting on a monthly retainer. Goal-setting, channel strategy, hiring/team support, agency selection, vendor management.",
        "Typical Engagement Length": "Ongoing",
        "Default Phase Sequence": "Active (rolling monthly)",
        "Default Deliverables": "Monthly strategy session · Async Slack support · Quarterly review",
        "Standard Pricing Notes": "Monthly retainer.",
        "Active": True,
        "Notes": "Retainer — one rolling Project per month per client (Project name convention: '{Client} — Marketing Consulting — {Mon YYYY}').",
    },
]


RISK_PROFILES_SEEDS = [
    # E-commerce
    {
        "Pattern Name": "Acknowledgment Gap",
        "Segment": "E-commerce",
        "Severity Default": "High",
        "Threshold Value": 5,
        "Threshold Unit": "business days",
        "Window": "rolling 7 days",
        "Description": "Client hasn't acknowledged a deliverable or message in N business days.",
        "Active": True,
    },
    {
        "Pattern Name": "Silent After Deliverable",
        "Segment": "E-commerce",
        "Severity Default": "High",
        "Threshold Value": 5,
        "Threshold Unit": "business days",
        "Window": "since last delivery",
        "Description": "We delivered something to the client and they've gone silent for N days — risk that the deliverable missed the mark.",
        "Active": True,
    },
    {
        "Pattern Name": "Klaviyo List Decline",
        "Segment": "E-commerce",
        "Severity Default": "Medium",
        "Threshold Value": 10,
        "Threshold Unit": "%",
        "Window": "rolling 8 weeks",
        "Description": "Client's email list size declined by >N% over the rolling window.",
        "Active": True,
    },
    {
        "Pattern Name": "ROAS Trend Down",
        "Segment": "E-commerce",
        "Severity Default": "High",
        "Threshold Value": 20,
        "Threshold Unit": "%",
        "Window": "rolling 8 weeks",
        "Description": "Client's blended ROAS declined by >N% vs the rolling baseline.",
        "Active": True,
    },
    # Local Service
    {
        "Pattern Name": "Acknowledgment Gap",
        "Segment": "Local Service",
        "Severity Default": "High",
        "Threshold Value": 5,
        "Threshold Unit": "business days",
        "Window": "rolling 7 days",
        "Description": "Client hasn't acknowledged a deliverable or message in N business days.",
        "Active": True,
    },
    {
        "Pattern Name": "GBP Reviews Dropped",
        "Segment": "Local Service",
        "Severity Default": "Medium",
        "Threshold Value": 2,
        "Threshold Unit": "count",
        "Window": "rolling 30 days",
        "Description": "Client's GBP review velocity dropped by N or more reviews/month.",
        "Active": True,
    },
    {
        "Pattern Name": "Lead Volume Decline",
        "Segment": "Local Service",
        "Severity Default": "High",
        "Threshold Value": 25,
        "Threshold Unit": "%",
        "Window": "rolling 8 weeks",
        "Description": "Tracked lead volume declined by >N% vs the rolling baseline.",
        "Active": True,
    },
    # Agency Partner
    {
        "Pattern Name": "Acknowledgment Gap",
        "Segment": "Agency Partner",
        "Severity Default": "High",
        "Threshold Value": 5,
        "Threshold Unit": "business days",
        "Window": "rolling 7 days",
        "Description": "Partner hasn't acknowledged a deliverable or message in N business days.",
        "Active": True,
    },
    {
        "Pattern Name": "White-label Hand-off Failed",
        "Segment": "Agency Partner",
        "Severity Default": "Critical",
        "Threshold Value": 0,
        "Threshold Unit": "",
        "Window": "per delivery",
        "Description": "A white-label deliverable was returned for revision more than once — partner unhappy.",
        "Active": True,
    },
    {
        "Pattern Name": "Stakeholder Change",
        "Segment": "Agency Partner",
        "Severity Default": "Medium",
        "Threshold Value": 0,
        "Threshold Unit": "",
        "Window": "per detection",
        "Description": "New decision-maker detected at the partner's end (signature change, 'John reports to Sarah now', etc.).",
        "Active": True,
    },
]


TEAM_SEEDS = [
    {
        "Name": "the implementer",
        "Workspace Email": "owner@example.com",
        "Role": "Leadership",
        "Active": True,
        "Capacity Hours per Week": 40,
        "Notes": "Co-founder, Brain build owner.",
    },
]


def insert_records(base_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
    """POST to /v0/{base}/{table} with records[].fields. Up to 10 records per call."""
    url = f"{API_ROOT}/{base_id}/{urllib.parse.quote(table_name)}"
    for i in range(0, len(rows), 10):
        chunk = rows[i : i + 10]
        body = {"records": [{"fields": r} for r in chunk]}
        resp = _request("POST", url, body)
        print(f"    seeded {table_name}: +{len(resp.get('records', []))} rows")
        time.sleep(0.25)


def seed_data(base_id: str) -> None:
    print("==> Seeding configuration tables")
    insert_records(base_id, "Service Catalog", SERVICE_CATALOG_SEEDS)
    insert_records(base_id, "Risk Profiles", RISK_PROFILES_SEEDS)
    insert_records(base_id, "Team", TEAM_SEEDS)


# ---- main --------------------------------------------------------------------


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text())
    abort_if_base_exists()
    base_id, table_ids, field_ids = create_base_with_first_table(schema)
    create_remaining_tables(base_id, schema, table_ids, field_ids)
    add_link_fields(base_id, schema, table_ids, field_ids)
    add_lookup_and_count_fields(base_id, schema, table_ids, field_ids)
    seed_data(base_id)

    print()
    print("=" * 64)
    print(f"DONE.  base_id = {base_id}")
    print()
    if SKIPPED_FIELDS_LOG:
        print("Fields the Meta API can't create — add manually in Airtable UI:")
        for line in SKIPPED_FIELDS_LOG:
            print(line)
        print()
    print("Next steps:")
    print("  1. Add to terraform/envs/prod/terraform.tfvars:")
    print(f"     airtable_base_id = {base_id!r}")
    print("  2. Add the manual fields listed above (createdTime / lastModifiedTime / count)")
    print("  3. Build views in Airtable (15 min, optional but valuable)")
    print("  4. REVOKE the bootstrap PAT now — it's no longer needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
