"""Daily spend check (ADR 0030, daily 09:00 UTC).

Runs as Cloud Run Job ``asb-audit-cost-daily-check``. Queries the
billing-account-scoped BQ billing export for the previous calendar day's
cost grouped by project + service, restricted to the three monitored
projects, posts a Chat summary to the ``Brain alerts`` space, and emits
``COST_THRESHOLD_EXCEEDED`` if any project exceeded its configured
threshold.

Why a daily Cloud Run Job instead of a native budget alert: GCP Billing
Budgets are calendar-monthly only (``calendar_period`` is MONTH/QUARTER/YEAR;
``custom_period`` is one-shot, not recurring). A true daily threshold
requires querying the export ourselves.

Required env vars (set by Terraform in
``terraform/modules/security/runtime_audits.tf``):

- ``BRAIN_PROJECT_ID``
- ``AUDIT_SA_EMAIL``
- ``BILLING_EXPORT_DATASET`` — defaults to ``billing_export``
- ``COST_THRESHOLDS_USD`` — JSON map of ``{project_id: threshold}`` (USD).
  Projects not in the map get visibility-only treatment (still in the Chat
  ping if present in the query results, but no breach signal).
- ``CHAT_WEBHOOK_SECRET_ID`` — defaults to ``second-brain-gchat-webhook``.

Caveat: BQ billing export typically lags 6-24h. Running at 09:00 UTC
reports the previous calendar day's cost as observed at that time —
i.e. the latest day with finalized data may be 1-2 days back, not
"yesterday." If the export returns no rows for the queried window,
the script emits ``COST_AUDIT_OK`` with ``note=billing_export_empty``
rather than treating that as a breach.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..common.audit_log import AuditLogClient
from ._reporter import AuditScriptContext, DriftReporter

log = logging.getLogger("agency_brain.audit.daily_spend_check")

DEFAULT_BILLING_DATASET = "billing_export"
DEFAULT_CHAT_WEBHOOK_SECRET = "second-brain-gchat-webhook"  # noqa: S105


# ---------------------------------------------------------------------------
# BigQuery query
# ---------------------------------------------------------------------------

# Sums net cost (cost minus credits) over the previous 24h window, grouped by
# project and service. Restricted to the configured monitored projects so
# unrelated projects on the same billing account don't pad the response.
QUERY_TEMPLATE = """
SELECT
  project.id AS project_id,
  service.description AS service,
  ROUND(SUM(cost) + COALESCE(SUM((SELECT SUM(c.amount) FROM UNNEST(credits) c)), 0), 4) AS net_cost_usd
FROM `{project}.{dataset}.gcp_billing_export_v1_*`
WHERE
  project.id IN UNNEST(@project_ids)
  AND _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
  AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND usage_start_time <  CURRENT_TIMESTAMP()
GROUP BY project_id, service
HAVING net_cost_usd > 0
ORDER BY project_id, net_cost_usd DESC
"""


def query_spend(
    *,
    project_id: str,
    dataset: str,
    monitored_project_ids: list[str],
    bq_client: Any,
) -> dict[str, dict[str, Any]]:
    """Return ``{project_id: {"total": float, "services": {svc: cost}}}``.

    Uses a parameterized query to avoid string interpolation of the project
    list. Projects with no rows in the window are absent from the result —
    callers should treat absence as "0 / no data" not as an error.
    """
    from google.cloud import bigquery

    sql = QUERY_TEMPLATE.format(project=project_id, dataset=dataset)
    job = bq_client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("project_ids", "STRING", monitored_project_ids),
            ],
        ),
    )

    out: dict[str, dict[str, Any]] = {}
    for row in job.result():
        bucket = out.setdefault(row["project_id"], {"total": 0.0, "services": {}})
        cost = float(row["net_cost_usd"])
        bucket["services"][row["service"]] = cost
        bucket["total"] = round(bucket["total"] + cost, 4)
    return out


# ---------------------------------------------------------------------------
# Threshold evaluation
# ---------------------------------------------------------------------------


def evaluate_thresholds(
    spend: dict[str, dict[str, Any]],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    """Return one entry per project that exceeded its threshold."""
    breaches: list[dict[str, Any]] = []
    for project_id, threshold in thresholds.items():
        bucket = spend.get(project_id)
        if bucket is None:
            continue
        if bucket["total"] > threshold:
            breaches.append(
                {
                    "project_id": project_id,
                    "total_usd": bucket["total"],
                    "threshold_usd": threshold,
                    "top_services": sorted(
                        bucket["services"].items(),
                        key=lambda kv: kv[1],
                        reverse=True,
                    )[:5],
                }
            )
    return breaches


# ---------------------------------------------------------------------------
# Chat formatting + posting
# ---------------------------------------------------------------------------


def format_chat_message(
    *,
    spend: dict[str, dict[str, Any]],
    thresholds: dict[str, float],
    breaches: list[dict[str, Any]],
    window_label: str,
) -> dict[str, Any]:
    """Build a Chat ``cardsV2`` payload summarizing the run.

    Designed to be readable in a ~30s morning glance: one line per project,
    threshold for monitored projects shown inline, breaches called out
    above the body.
    """
    lines: list[str] = []
    if breaches:
        lines.append(f"<b>⚠ {len(breaches)} project(s) over threshold</b>")
        for b in breaches:
            lines.append(
                f"• <b>{b['project_id']}</b>: "
                f"${b['total_usd']:.2f} "
                f"(threshold ${b['threshold_usd']:.2f})"
            )
        lines.append("")

    if not spend:
        lines.append("<i>No billing data for this window — export may not be populated yet.</i>")
    else:
        lines.append(f"<b>Spend, {window_label}:</b>")
        for project_id in sorted(spend, key=lambda p: -spend[p]["total"]):
            total = spend[project_id]["total"]
            t = thresholds.get(project_id)
            tag = f"  (≤ ${t:.0f} target)" if t is not None else ""
            lines.append(f"• <b>{project_id}</b>: ${total:.2f}{tag}")

    text = "\n".join(lines)
    return {
        "cardsV2": [
            {
                "cardId": "asb-cost-daily-check",
                "card": {
                    "header": {
                        "title": "Daily spend check",
                        "subtitle": "Agency Second Brain · ADR 0030",
                    },
                    "sections": [{"widgets": [{"textParagraph": {"text": text}}]}],
                },
            }
        ]
    }


def load_webhook_url(*, project_id: str, secret_id: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


def post_to_chat(*, url: str, payload: dict[str, Any], timeout_s: float = 10.0) -> int:
    """POST the cardsV2 payload to the Chat webhook. Returns HTTP status."""
    data = json.dumps(payload).encode("utf-8")
    # URL is the Chat incoming-webhook from Secret Manager — always
    # https://chat.googleapis.com/... — not user-controlled.
    req = Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            return resp.status
    except HTTPError as exc:
        log.warning("chat webhook returned HTTP %d", exc.code)
        return exc.code
    except URLError as exc:
        log.warning("chat webhook unreachable: %s", exc)
        return -1


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )
    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "AUDIT_SA_EMAIL",
        f"asb-audit-cost-daily@{project_id}.iam.gserviceaccount.com",
    )
    dataset = os.environ.get("BILLING_EXPORT_DATASET", DEFAULT_BILLING_DATASET)
    webhook_secret = os.environ.get("CHAT_WEBHOOK_SECRET_ID", DEFAULT_CHAT_WEBHOOK_SECRET)

    thresholds_raw = os.environ.get("COST_THRESHOLDS_USD", "{}")
    try:
        thresholds_in = json.loads(thresholds_raw)
        thresholds: dict[str, float] = {k: float(v) for k, v in thresholds_in.items()}
    except (ValueError, TypeError) as exc:
        log.error("invalid COST_THRESHOLDS_USD env (%s): %r", exc, thresholds_raw)
        thresholds = {}

    audit_log = AuditLogClient(project_id=project_id)
    ctx = AuditScriptContext(
        short_name="cost-daily-check",
        sa_email=sa_email,
        project_id=project_id,
        audit_log=audit_log,
        started_perf=time.perf_counter(),
    )
    reporter = DriftReporter(ctx)

    monitored = sorted(thresholds.keys()) or [project_id]
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=24)
    window_label = f"{window_start:%Y-%m-%d %H:%M} → {window_end:%Y-%m-%d %H:%M} UTC"

    try:
        from google.cloud import bigquery

        # billing_export lives in the brain project (US multi-region per ADR 0013).
        bq_client = bigquery.Client(project=project_id, location="US")
        spend = query_spend(
            project_id=project_id,
            dataset=dataset,
            monitored_project_ids=monitored,
            bq_client=bq_client,
        )
    except Exception as exc:
        log.exception("daily_spend_check query failed")
        reporter.error(exc)
        return 1

    breaches = evaluate_thresholds(spend, thresholds)

    # Always post a Chat summary, regardless of breach. Failure to post is
    # a warning, not a fatal — the BQ audit row + structured stdout are the
    # canonical record, and a webhook outage shouldn't suppress the alert
    # path either (the log-based metric below fires on the stdout event).
    try:
        url = load_webhook_url(project_id=project_id, secret_id=webhook_secret)
        chat_payload = format_chat_message(
            spend=spend,
            thresholds=thresholds,
            breaches=breaches,
            window_label=window_label,
        )
        status = post_to_chat(url=url, payload=chat_payload)
        if not (200 <= status < 300):
            log.warning("chat post non-2xx status=%d", status)
    except Exception:
        log.exception("chat post failed (non-fatal)")

    summary = {
        "window": window_label,
        "monitored_projects": monitored,
        "thresholds_usd": thresholds,
        "totals_usd": {p: spend[p]["total"] for p in spend},
        "breach_count": len(breaches),
    }

    if not spend:
        summary["note"] = "billing_export_empty"
        reporter.cost_ok(summary)
        return 0

    if breaches:
        summary["breaches"] = breaches
        reporter.cost_breach(summary)
        return 2

    reporter.cost_ok(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
