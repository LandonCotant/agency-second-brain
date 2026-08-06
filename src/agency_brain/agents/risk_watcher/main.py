"""Cloud Run Job entrypoint for the WS-G2 Risk Watcher.

Run shape (one Cloud Run Job execution per scheduler tick), per
ADR 0033 (e-comm) + ADR 0034 (multi-segment loop):

  1. Per segment in ``[ECOMMERCE, LOCAL_SERVICE]`` (and AGENCY_PARTNER
     once PR-E lands):
     a. Pull thresholds from ``airtable_replica.risk_profiles``.
     b. Build the profile via the segment's factory.
     c. Skip the segment if ``profile.signals`` is empty (AP today).
     d. Pull active accounts + per-account rollups via the segment's
        loader.
     e. Invoke ``RiskWatcher.invoke`` once over all states (one audit
        row per segment per tick — granular per-flag signal data
        lives in ``agent_outputs.risk_flags`` rows).
     f. Write each fired flag via ``RiskFlagsWriter`` (same-day
        dedup pre-check; ADR 0033 §4).
  2. Exit 0 on success, 1 if any flag write failed in any segment.

A single segment's loader/invoke failure is logged but doesn't poison
the next segment — the loop carries `failures` across iterations.

Required env vars:
  - ``BRAIN_PROJECT_ID``
  - ``RISK_WATCHER_SA_EMAIL`` (this Job runs as
    ``asb-risk-watcher-sa``; defaulted from project id when unset)

Optional env vars:
  - ``MODEL_NAME`` — recorded on each flag row for cost dashboards;
    no LLM call in PR-D (signals are deterministic).
  - ``PROMPT_VERSION`` — git short-sha or label; recorded on each
    flag row.
  - ``RISK_WATCHER_DWD_TARGET_SA`` — the DWD-grantable SA to
    impersonate for ``calendar.readonly`` (ADR 0034 §4). Defaults to
    ``asb-agent-triage-sa@<project>.iam.gserviceaccount.com``.
  - ``RISK_WATCHER_OWNER_CALENDAR_SUBJECT`` — DWD subject for the
    Calendar reads (the agency owner whose calendar we read). Defaults
    to ``owner@example.com``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("agency_brain.agents.risk_watcher.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "RISK_WATCHER_SA_EMAIL",
        f"asb-risk-watcher-sa@{project_id}.iam.gserviceaccount.com",
    )
    model_name = os.environ.get("MODEL_NAME", "deterministic")
    prompt_version = os.environ.get("PROMPT_VERSION", "v1")
    dwd_target_sa = os.environ.get(
        "RISK_WATCHER_DWD_TARGET_SA",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    )
    owner_calendar_subject = os.environ.get(
        "RISK_WATCHER_OWNER_CALENDAR_SUBJECT",
        "owner@example.com",
    )

    log.info("risk_watcher.start project=%s sa=%s", project_id, sa_email)

    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.memory_bank import InMemoryMemoryBank
    from .agency_partner_profile import build_agency_partner_profile
    from .base import RiskWatcher
    from .ecommerce_profile import build_ecommerce_profile
    from .loaders import (
        AgencyPartnerClientStateLoader,
        EcommerceClientStateLoader,
        LocalServiceClientStateLoader,
        PersonalClientStateLoader,
        RiskProfileThresholdsLoader,
    )
    from .local_service_profile import build_local_service_profile
    from .models import Segment
    from .personal_profile import build_personal_profile
    from .writer import RiskFlagsWriter

    bq_client = bigquery.Client(project=project_id)
    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)
    bq_rows = _ParameterizedQueryAdapter(bq_client)

    thresholds_loader = RiskProfileThresholdsLoader(bq=bq_rows, project_id=project_id)
    writer = RiskFlagsWriter(
        rows_client=_BQRowsInsertAdapter(bq_client),
        query_client=bq_rows,
        project_id=project_id,
    )
    watcher = RiskWatcher(
        agent_id="risk-watcher",
        sa_email=sa_email,
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        # Profile is rebuilt per segment inside the loop; this is a
        # placeholder that gets overwritten before each ``invoke``.
        profile=build_ecommerce_profile(),
    )

    calendar_client = _build_calendar_client(
        target_sa=dwd_target_sa,
        owner_subject=owner_calendar_subject,
    )

    segments: list[tuple[Segment, Any, Any]] = [
        (
            Segment.ECOMMERCE,
            build_ecommerce_profile,
            lambda: EcommerceClientStateLoader(bq=bq_rows, project_id=project_id),
        ),
        (
            Segment.LOCAL_SERVICE,
            build_local_service_profile,
            lambda: LocalServiceClientStateLoader(
                bq=bq_rows,
                project_id=project_id,
                calendar=calendar_client,
            ),
        ),
        (
            Segment.AGENCY_PARTNER,
            build_agency_partner_profile,
            lambda: AgencyPartnerClientStateLoader(bq=bq_rows, project_id=project_id),
        ),
        (
            Segment.PERSONAL,
            build_personal_profile,
            lambda: PersonalClientStateLoader(bq=bq_rows, project_id=project_id),
        ),
    ]

    failures = 0
    for segment, profile_factory, loader_factory in segments:
        try:
            failures += _run_segment(
                segment=segment,
                profile_factory=profile_factory,
                loader_factory=loader_factory,
                thresholds_loader=thresholds_loader,
                watcher=watcher,
                writer=writer,
                model_name=model_name,
                prompt_version=prompt_version,
            )
        except Exception:
            log.exception("risk_watcher.segment_failed segment=%s", segment.value)
            failures += 1

    log.info("risk_watcher.done failures=%d", failures)
    return 0 if failures == 0 else 1


def _run_segment(
    *,
    segment: Any,
    profile_factory: Any,
    loader_factory: Any,
    thresholds_loader: Any,
    watcher: Any,
    writer: Any,
    model_name: str,
    prompt_version: str,
) -> int:
    """Run one segment of the multi-segment tick. Returns flag-write failures."""
    thresholds = thresholds_loader.load(segment)
    profile = profile_factory(thresholds)
    if not profile.signals:
        log.info(
            "risk_watcher.skipped segment=%s reason=no_signals",
            segment.value,
        )
        return 0

    loader = loader_factory()
    states = loader.load()
    log.info(
        "risk_watcher.loaded segment=%s accounts=%d thresholds=%d",
        segment.value,
        len(states),
        len(thresholds),
    )

    watcher.profile = profile
    output = watcher.invoke(_build_input(states))

    if not output.flags:
        log.info(
            "risk_watcher.quiet segment=%s flags=0 evaluated=%d",
            segment.value,
            output.accounts_evaluated,
        )
        return 0

    failures = 0
    for flag in output.flags:
        row = watcher.materialize_flag_row(flag, model=model_name, prompt_version=prompt_version)
        try:
            result = writer.write(row)
        except Exception:
            failures += 1
            log.exception(
                "risk_watcher.flag_write_failed segment=%s account=%s pattern=%s",
                segment.value,
                flag.account_id,
                flag.pattern_name,
            )
            continue
        if result.suppressed:
            # ADR 0060 §3 — operator muted this (account, pattern). The flag
            # was written pre-resolved (invisible to brief + fan-out); log it
            # so the suppression is observable in Cloud Logging, never silent.
            log.info(
                "risk_watcher.flag_suppressed segment=%s account=%s pattern=%s "
                "flag_id=%s feedback_id=%s",
                segment.value,
                flag.account_id,
                flag.pattern_name,
                result.flag_id,
                result.suppressed_by,
            )
        else:
            log.info(
                "risk_watcher.flag_recorded segment=%s account=%s pattern=%s flag_id=%s written=%s",
                segment.value,
                flag.account_id,
                flag.pattern_name,
                result.flag_id,
                result.written,
            )
    return failures


def _build_input(states: tuple[Any, ...]) -> Any:
    from .models import RiskWatcherInput

    return RiskWatcherInput(states=states)


def _build_calendar_client(*, target_sa: str, owner_subject: str) -> Any:
    """Wire the DWD-impersonated CalendarClient.

    Returns ``None`` if the google-api libs aren't importable (test /
    constrained envs); the LS loader handles that by reporting a
    quiet calendar state and skipping Owner Disengagement.
    """
    try:
        from ...common.dwd import DWDServiceFactory
        from .calendar_client import CALENDAR_READONLY_SCOPE

        factory = DWDServiceFactory(
            target_principal=target_sa,
            scope=CALENDAR_READONLY_SCOPE,
            api="calendar",
            api_version="v3",
        )
        return _SubjectPinnedCalendar(
            factory=factory,
            owner_subject=owner_subject,
        )
    except Exception:
        log.exception("risk_watcher.calendar_client_init_failed")
        return None


# ----------------------------------------------------- adapters


class _SubjectPinnedCalendar:
    """Wraps ``CalendarClient`` with a fixed DWD subject (the agency owner).

    The LS loader passes ``owner_email`` as the calendar to read, but
    we always read the agency owner's calendar — the ``owner_email``
    is also the DWD subject. This wrapper lets the factory be built
    once and reused per account, AND authoritatively uses the
    configured subject so a misconfigured Account.account_manager
    mapping can't cause us to read a calendar we don't have DWD
    access to.
    """

    def __init__(self, *, factory: Any, owner_subject: str) -> None:
        from .calendar_client import CalendarClient

        self._client = CalendarClient(service_factory=factory)
        self._owner_subject = owner_subject

    def most_recent_engagement_event(
        self,
        *,
        owner_email: str,
        attendee_emails: tuple[str, ...],
        since: Any,
        until: Any,
    ) -> Any:
        # owner_email is ignored (we pin the configured subject); the
        # parameter is kept to satisfy the CalendarRecencyClient
        # Protocol shape.
        del owner_email
        return self._client.most_recent_engagement_event(
            owner_email=self._owner_subject,
            attendee_emails=attendee_emails,
            since=since,
            until=until,
        )


class _ParameterizedQueryAdapter:
    """Dual-mode query client over ``google.cloud.bigquery.Client``.

    The loaders use the no-parameter ``query_rows(sql)`` shape. The
    writer's dedup pre-check uses the parameterized
    ``query_rows(sql, parameters)`` shape. This adapter accepts
    either, so a single instance serves both.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        if not parameters:
            return [dict(row.items()) for row in self._bq.query(sql).result()]

        from google.cloud import bigquery

        sql_params: list = []
        for p in parameters:
            sql_params.append(bigquery.ScalarQueryParameter(p["name"], p["type"], p["value"]))
        job_config = bigquery.QueryJobConfig(query_parameters=sql_params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


class _BQRowsInsertAdapter:
    """Implements ``writer.BQRowsClient`` over the SDK Client."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._bq.insert_rows_json(table_ref, rows)


if __name__ == "__main__":
    sys.exit(main())
