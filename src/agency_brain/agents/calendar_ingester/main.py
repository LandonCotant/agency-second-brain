"""Cloud Run Job entrypoint for the Calendar ingester.

One invocation per day at 06:30 PT. Process:
  1. Load HIPAA domain set from BQ.
  2. Resolve lookback window: now - 180d → now + 90d (configurable).
  3. List events from each configured calendar_id (default: 'primary').
  4. Filter HIPAA-flagged events.
  5. Transform each event → NoteRow (embeds via VertexEmbedder).
  6. MERGE into agent_outputs.notes (idempotent on external_id).
  7. Emit one audit row with per-tick stats.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime, timedelta

log = logging.getLogger("agency_brain.agents.calendar_ingester.main")
logging.basicConfig(level=logging.INFO)


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val or ""


def parse_calendar_configs(csv: str, default_scope: str) -> list[tuple[str, str]]:
    """Parse CALENDAR_IDS CSV into [(calendar_id, scope), ...].

    Each comma-separated item is either ``"id"`` (uses ``default_scope``)
    or ``"id:scope"`` (uses the explicit scope). Empty items are skipped.
    Items with more than one ``:`` keep the rightmost segment as scope
    so calendar IDs containing a colon (rare but possible) still parse.

    >>> parse_calendar_configs("primary", "agency")
    [('primary', 'agency')]
    >>> parse_calendar_configs("primary,personal@example.com:personal", "agency")
    [('primary', 'agency'), ('personal@example.com', 'personal')]
    """
    out: list[tuple[str, str]] = []
    for raw in csv.split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" in item:
            cid, _, scope = item.rpartition(":")
            cid = cid.strip()
            scope = scope.strip() or default_scope
            if cid:
                out.append((cid, scope))
        else:
            out.append((item, default_scope))
    return out


def main(argv: list[str] | None = None) -> int:
    from google.cloud import bigquery

    from ...common.dwd import DWDServiceFactory
    from ..notes_ingestor.embedder import VertexEmbedder
    from .calendar_client import CALENDAR_READONLY_SCOPE, CalendarClient
    from .hipaa_filter import CalendarHipaaFilter, load_hipaa_domains_from_bq
    from .models import IngestResult
    from .transformer import transform_event
    from .writer import CalendarWriter

    project_id = _env("BRAIN_PROJECT_ID", required=True)
    target_principal = _env(
        "CALENDAR_DWD_TARGET_PRINCIPAL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )
    subject = _env("CALENDAR_DWD_SUBJECT", "owner@example.com")
    calendar_ids_csv = _env("CALENDAR_IDS", "primary")
    lookback_days = int(_env("CALENDAR_LOOKBACK_DAYS", "180"))
    lookahead_days = int(_env("CALENDAR_LOOKAHEAD_DAYS", "90"))
    location = _env("CALENDAR_VERTEX_LOCATION", "us-central1")
    scope_value = _env("CALENDAR_NOTES_SCOPE", "agency")

    calendar_configs = parse_calendar_configs(calendar_ids_csv, scope_value)

    bq_client = bigquery.Client(project=project_id)
    bq_query_adapter = _BQQueryAdapter(bq_client)

    # HIPAA domains.
    hipaa_domains = load_hipaa_domains_from_bq(bq_query=bq_query_adapter, project_id=project_id)
    hipaa_filter = CalendarHipaaFilter(hipaa_domains=hipaa_domains)

    factory = DWDServiceFactory(
        target_principal=target_principal,
        scope=CALENDAR_READONLY_SCOPE,
        api="calendar",
        api_version="v3",
    )
    calendar_client = CalendarClient(factory=factory, subject=subject)

    embedder = VertexEmbedder(project_id=project_id, location=location)
    writer = CalendarWriter(bq_query=bq_query_adapter, project_id=project_id)

    now = datetime.now(UTC)
    time_min = now - timedelta(days=lookback_days)
    time_max = now + timedelta(days=lookahead_days)

    total_events = 0
    skipped_hipaa = 0
    embed_failures: list[int] = [0]
    rows = []

    for cid, cal_scope in calendar_configs:
        try:
            events = calendar_client.list_events(
                calendar_id=cid, time_min=time_min, time_max=time_max
            )
        except Exception:
            log.exception("calendar_ingester.main.list_failed calendar_id=%s", cid)
            continue
        log.info(
            "calendar_ingester.main.listed calendar_id=%s scope=%s count=%d",
            cid,
            cal_scope,
            len(events),
        )
        for event in events:
            total_events += 1
            check = hipaa_filter.check(event)
            if not check.allowed:
                skipped_hipaa += 1
                log.info(
                    "calendar_ingester.main.hipaa_skip event_id=%s offenders=%s",
                    event.event_id,
                    check.blocking_attendees,
                )
                continue
            note_row = transform_event(
                event=event,
                embedder=embedder,
                scope=cal_scope,
                embed_failures_counter=embed_failures,
            )
            if note_row is not None:
                rows.append(note_row)

    merge_result = writer.merge(rows)
    final = IngestResult(
        total_events=total_events,
        inserted=merge_result.inserted,
        updated=merge_result.updated,
        unchanged=merge_result.unchanged,
        skipped_hipaa=skipped_hipaa,
        embed_failures=embed_failures[0],
        errors=merge_result.errors,
    )
    log.info(
        "calendar_ingester.main.done total=%d inserted=%d updated=%d "
        "unchanged=%d skipped_hipaa=%d embed_failures=%d errors=%d",
        final.total_events,
        final.inserted,
        final.updated,
        final.unchanged,
        final.skipped_hipaa,
        final.embed_failures,
        final.errors,
    )
    return 0 if final.errors == 0 else 1


class _BQQueryAdapter:
    def __init__(self, client) -> None:
        self._client = client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
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
        return [dict(r) for r in self._client.query(sql, job_config=job_config).result()]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
