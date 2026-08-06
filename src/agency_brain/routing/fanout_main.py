"""Cloud Run Job entrypoint for ``asb-routing-fanout`` (ADR 0023, ADR 0025, ADR 0032, ADR 0041).

Wires the production clients to :func:`run_fanout_tick`. Each tick polls
three sources and dispatches each matched row to every matrix-routed
channel that has a registered adapter and is not already recorded in
``agent_outputs.routed_events``: triaged_items (5-min lookback),
risk_flags (24h lookback), and decisions WHERE status='draft' (24h
lookback, ADR 0041). One ``routed_events`` row is INSERTed per
successful per-channel dispatch. Cloud Scheduler runs this every 5
minutes; this module is invoked once per tick and exits.

Env vars (set by ``terraform/modules/agent_runtime/routing_fanout.tf``):

- ``BRAIN_PROJECT_ID``
- ``ROUTING_SA_EMAIL`` — for the audit row's ``sa_email`` column
- ``LOOKBACK_MINUTES`` — how far back to scan triaged_items (default 30)
- ``RISK_FLAGS_LOOKBACK_MINUTES`` — risk_flags lookback (default 1440 = 24h)
- ``DECISIONS_LOOKBACK_MINUTES`` — decisions lookback (default 1440 = 24h)
- ``CHAT_WEBHOOK_SECRET_ID`` — Secret Manager short name (default
  ``second-brain-gchat-webhook``); empty disables Chat dispatch
- ``CHAT_WEBHOOK_SECRET_VERSION`` — default ``latest``
- ``TRIAGE_SA_EMAIL`` — DWD impersonation target for the Gmail draft
  channel (ADR 0032). Defaults to
  ``asb-agent-triage-sa@${BRAIN_PROJECT_ID}.iam.gserviceaccount.com``.
  Empty disables the Gmail channel.
- ``GMAIL_DRAFT_RECIPIENT`` — recipient mailbox for drafts (default
  ``owner@example.com``)
- ``DRY_RUN`` — ``true`` runs poll + dispatch decisions but skips the
  Chat POST, the Gmail drafts.create, and the routed_events insert
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from ..common.audit_log import AuditLogClient
from ..common.memory_bank import InMemoryMemoryBank
from .channels.chat import ChatWebhookClient, UrllibPoster
from .channels.gmail import GmailDraftClient, GmailDraftResult
from .fanout import (
    ACTIVE_CHANNELS,
    BQQueryClient,
    ChannelClient,
    FanoutInput,
    RoutingFanoutAgent,
    make_chat_adapter,
    make_gmail_adapter,
    run_fanout_tick,
)
from .formatters import RoutingMessageContext
from .matrix import Channel, TriagedItem
from .polling import (
    build_decisions_poll_query,
    build_risk_flags_poll_query,
    build_triaged_items_poll_query,
)

log = logging.getLogger("agency_brain.routing.fanout_main")

DEFAULT_SECRET_ID = "second-brain-gchat-webhook"  # noqa: S105 — secret name, not the secret itself
DEFAULT_SECRET_VERSION = "latest"  # noqa: S105 — Secret Manager version alias
DEFAULT_LOOKBACK_MINUTES = 30
# Risk Watcher fires once a day; the routing fan-out (every 5 min)
# needs a long lookback so a flag fired at 06:00 PT is dispatched to
# Chat once the 09:00 PT severity window opens, even after multiple
# intermediate ticks. Per-channel routed_events dedup means re-ticks
# don't double-dispatch.
DEFAULT_RISK_FLAGS_LOOKBACK_MINUTES = 1440  # 24h
# ADR 0041: decisions are draft until the user fills in alternatives +
# prediction + confidence. 24h lookback gives the every-5-min routing
# tick plenty of recovery headroom; per-channel routed_events dedup
# means a draft is dispatched at most once per channel.
DEFAULT_DECISIONS_LOOKBACK_MINUTES = 1440  # 24h
# ADR 0041: decisions don't have a severity column. We synthesize "high"
# in the row converter so the existing Chat windowing (09:00–16:00 PT,
# ADR 0023) gives the daily-digest UX without a separate Cloud Run Job:
# a draft created at 9pm sits idle until the next 09:00 PT tick fires
# the Chat card. Gmail drafts always fire (they don't notify).
DECISIONS_SYNTHETIC_SEVERITY = "high"
# ADR 0041: leadership-only routing in v1 (matches risk_flags). The
# draft lands in owner@example.com via the existing
# leadership_email default in routing.matrix.route_item.
DECISIONS_OWNER_TYPE = "leadership"
DEFAULT_GMAIL_RECIPIENT = "owner@example.com"


# ---------------------------------------------------------------------------
# Production BQ client
# ---------------------------------------------------------------------------


class BigQueryFanoutClient:
    """Production :class:`BQQueryClient` backed by ``google.cloud.bigquery``."""

    def __init__(self, project_id: str, bq_client: Any = None) -> None:
        self._project_id = project_id
        self._client = bq_client
        self._routed_events_table = f"{project_id}.agent_outputs.routed_events"

    def _bq(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self._project_id)
        return self._client

    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        job = self._bq().query(sql)
        return [dict(row) for row in job.result()]

    def record_routing(
        self,
        *,
        item_id: str,
        channel: str,
        chat_status: int | None = None,
        agent_run_id: str | None = None,
    ) -> int:
        """Insert a row into ``agent_outputs.routed_events`` (ADR 0025).

        Insert-only by design — BQ blocks DML on streaming-buffer rows
        for ~30 min after insert.
        """
        from datetime import UTC, datetime

        row = {
            "item_id": item_id,
            "channel": channel,
            "routed_at": datetime.now(UTC).isoformat(),
            "chat_status": chat_status,
            "agent_run_id": agent_run_id,
        }
        errors = self._bq().insert_rows_json(self._routed_events_table, [row])
        if errors:
            raise RuntimeError(f"BQ rejected routed_events insert: {errors}")
        return 1


# ---------------------------------------------------------------------------
# Secret Manager loader
# ---------------------------------------------------------------------------


def load_webhook_url(
    *,
    project_id: str,
    secret_id: str,
    version: str = DEFAULT_SECRET_VERSION,
) -> str:
    """Read the Chat webhook URL from Secret Manager.

    Returns the secret payload as a string; raises if the secret does
    not exist or the SA cannot access it.
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


# ---------------------------------------------------------------------------
# Row → FanoutInput converter
# ---------------------------------------------------------------------------


def row_to_input(row: dict[str, Any]) -> FanoutInput:
    routed_raw = row.get("routed_channels") or ()
    already_routed = frozenset(str(c) for c in routed_raw if c)
    return FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id=row["item_id"],
            severity=row["severity"],
            owner_type=row["owner_type"],
            owner_email=row.get("owner_email"),
            human_review_routed=row["human_review_routed"],
        ),
        triaged_at=row["triaged_at"],
        message_context=RoutingMessageContext(
            item_id=row["item_id"],
            severity=row["severity"],
            source=row["source"],
            action_type=row["action_type"],
            reasoning=row["reasoning"] or "",
            source_url=row.get("source_url"),
            source_event_ref=row.get("source_event_ref"),
            owner_email=row.get("owner_email"),
            airtable_task_record_id=row.get("airtable_task_record_id"),
            # ADR 0032: gmail_thread_id and subject are populated only
            # when the upstream WS-B PR-4 publisher stamps them. None
            # today for the existing synthetic Gmail signals.
            gmail_thread_id=row.get("gmail_thread_id"),
            subject=row.get("subject"),
        ),
        already_routed=already_routed,
    )


def risk_flag_row_to_input(row: dict[str, Any]) -> FanoutInput:
    """Convert one ``agent_outputs.risk_flags`` row to ``FanoutInput``.

    Maps risk_flag columns to the same shape ``RoutingFanoutAgent``
    consumes for triaged_items (ADR 0033 PR-C). Specifically:

    - ``flag_id`` → ``item_id`` everywhere (TriagedItem and
      RoutingMessageContext). ``routed_events.item_id`` is a generic
      source-id column; risk-flag dispatches write the flag_id there.
    - ``signal_evidence`` + ``reasoning`` are concatenated into the
      single ``reasoning`` field the formatters surface. Visually
      separated by a blank line.
    - ``source = "risk_watcher"``, which keeps the Gmail adapter's
      threading guard happy (only ``source == "gmail"`` rows attempt
      to thread; risk_flags carry no thread metadata).
    - ``subject`` is ``"{pattern_name} — {account_name}"`` when the
      LEFT JOIN to airtable_replica.accounts found a row; otherwise
      just the pattern name.
    - ``owner_type`` is ``"leadership"`` and ``owner_email`` is None
      so :func:`route_item` returns leadership-only RouteIntents.
      Owner-scoped routing is a future PR (account_manager → owner
      lookup); v1 mirrors ADR 0032's leadership-only Gmail recipient.
    """
    routed_raw = row.get("routed_channels") or ()
    already_routed = frozenset(str(c) for c in routed_raw if c)

    pattern_name = row["pattern_name"]
    account_id = row["account_id"]
    account_name = row.get("account_name")
    severity = row["severity"]

    evidence = (row.get("signal_evidence") or "").strip()
    reasoning_text = (row.get("reasoning") or "").strip()
    if evidence and reasoning_text:
        combined_reasoning = f"{evidence}\n\n{reasoning_text}"
    else:
        combined_reasoning = evidence or reasoning_text or "(no reasoning)"

    if account_name:
        subject = f"{pattern_name} — {account_name}"
    else:
        subject = pattern_name

    return FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id=row["flag_id"],
            severity=severity,
            owner_type="leadership",
            owner_email=None,
            human_review_routed=row["human_review_routed"],
        ),
        triaged_at=row["flagged_at"],
        message_context=RoutingMessageContext(
            item_id=row["flag_id"],
            severity=severity,
            source="risk_watcher",
            action_type=pattern_name,
            reasoning=combined_reasoning,
            source_url=None,
            source_event_ref=f"account={account_id}",
            owner_email=None,
            airtable_task_record_id=row.get("airtable_task_record_id"),
            gmail_thread_id=None,
            subject=subject,
        ),
        already_routed=already_routed,
    )


def make_decision_row_to_input(*, project_id: str) -> Any:
    """Bind the BQ project id into a row converter (ADR 0041).

    The Gmail draft body's UPDATE template references the fully-
    qualified ``decisions`` table (``{project}.agent_outputs.decisions``).
    Closing over ``project_id`` keeps the converter signature
    compatible with :func:`run_fanout_tick`'s ``row_to_input``
    contract (one positional dict argument).
    """

    def _convert(row: dict[str, Any]) -> FanoutInput:
        return decision_row_to_input(row, project_id=project_id)

    return _convert


def decision_row_to_input(
    row: dict[str, Any],
    *,
    project_id: str | None = None,
) -> FanoutInput:
    """Convert one ``agent_outputs.decisions`` (status='draft') row to ``FanoutInput`` (ADR 0041).

    Decisions are written by Captures Materializer (ADR 0039, prefix
    ``captures-decision-``) and Evening Reflection v2 (ADR 0040,
    prefix ``reflection-``). Both leave ``alternatives=[]``,
    ``prediction=NULL``, ``confidence=NULL`` for the user to refine.
    The fan-out surfaces them via Chat + Gmail draft so the user can
    fill in missing fields and flip ``status`` to ``pending`` (manual
    BQ console UPDATE in v1).

    Specifically:

    - ``decision_id`` flows into both ``TriagedItem.item_id`` and
      ``RoutingMessageContext.item_id``. ``routed_events.item_id``
      receives the ``decision_id`` at dispatch time so per-channel
      dedup works the same as for triaged_items + risk_flags.
    - Severity is synthesized as ``"high"`` so the existing Chat
      09:00–16:00 PT window applies (drafts created at 9pm sit until
      the next 09:00 PT tick). Gmail drafts always fire.
    - ``source = "decisions_reviewer"`` keeps the Gmail adapter's
      threading guard happy (only ``source == "gmail"`` rows attempt
      to thread) and triggers the formatters' decision-specific
      branches.
    - ``subject`` is ``"[DECISION DRAFT] {title}"``; the Gmail
      formatter uses it verbatim as the subject seed.
    - ``decision_title`` / ``decision_context`` / ``decision_choice``
      / ``source_voice_note_id`` are passed through to the formatters
      via the optional fields on :class:`RoutingMessageContext`.
    - ``project_id`` is stamped onto the context so the Gmail body's
      UPDATE template references the FQN
      ``{project}.agent_outputs.decisions``.
    """
    routed_raw = row.get("routed_channels") or ()
    already_routed = frozenset(str(c) for c in routed_raw if c)

    decision_id = row["decision_id"]
    title = (row.get("title") or "").strip()
    context_text = (row.get("context") or "").strip()
    choice_text = (row.get("choice") or "").strip()

    # Reasoning is the formatter's "preview" surface for the Chat card
    # title-line companion. Prefer context; fall back to choice.
    preview = context_text or choice_text or "(no context)"

    return FanoutInput(
        aspects=[],
        item=TriagedItem(
            item_id=decision_id,
            severity=DECISIONS_SYNTHETIC_SEVERITY,
            owner_type=DECISIONS_OWNER_TYPE,
            owner_email=None,
            human_review_routed=False,
        ),
        triaged_at=row["decided_at"],
        message_context=RoutingMessageContext(
            item_id=decision_id,
            severity=DECISIONS_SYNTHETIC_SEVERITY,
            source="decisions_reviewer",
            action_type="refine_decision",
            reasoning=preview,
            source_url=None,
            source_event_ref=None,
            owner_email=None,
            airtable_task_record_id=None,
            gmail_thread_id=None,
            subject=f"[DECISION DRAFT] {title}" if title else "[DECISION DRAFT]",
            decision_title=title or None,
            decision_context=context_text or None,
            decision_choice=choice_text or None,
            source_voice_note_id=row.get("source_voice_note_id"),
            bq_project_id=project_id,
        ),
        already_routed=already_routed,
    )


# ---------------------------------------------------------------------------
# DRY_RUN scaffolding
# ---------------------------------------------------------------------------


class _DryRunChatClient:
    """Stand-in for :class:`ChatWebhookClient` that logs instead of POSTing."""

    def send(self, text: str):
        log.info("[DRY_RUN] would send Chat message: %r", text[:200])
        from .channels.chat import ChatPostResult

        return ChatPostResult(ok=True, status=200, response_body="dry-run")


class _DryRunGmailClient:
    """Stand-in for :class:`GmailDraftClient` that logs instead of drafting."""

    def draft(
        self,
        *,
        recipient_email: str,
        subject: str,
        body_markdown: str,
        thread_id: str | None = None,
    ) -> GmailDraftResult:
        log.info(
            "[DRY_RUN] would draft Gmail: to=%s subject=%r threaded=%s body_len=%d",
            recipient_email,
            subject[:120],
            thread_id is not None,
            len(body_markdown),
        )
        return GmailDraftResult(ok=True, draft_id="dry-run", threaded=thread_id is not None)


class _DryRunBQ:
    """Wraps a real :class:`BigQueryFanoutClient` to skip writes."""

    def __init__(self, inner: BQQueryClient) -> None:
        self._inner = inner

    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        return self._inner.query_rows(sql)

    def record_routing(
        self,
        *,
        item_id: str,
        channel: str,
        chat_status: int | None = None,
        agent_run_id: str | None = None,
    ) -> int:
        log.info(
            "[DRY_RUN] would insert routed_events: item_id=%s channel=%s status=%s",
            item_id,
            channel,
            chat_status,
        )
        return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "ROUTING_SA_EMAIL",
        f"asb-routing-sa@{project_id}.iam.gserviceaccount.com",
    )
    secret_id = os.environ.get("CHAT_WEBHOOK_SECRET_ID", DEFAULT_SECRET_ID)
    secret_version = os.environ.get("CHAT_WEBHOOK_SECRET_VERSION", DEFAULT_SECRET_VERSION)
    lookback_minutes = int(os.environ.get("LOOKBACK_MINUTES", DEFAULT_LOOKBACK_MINUTES))
    risk_flags_lookback_minutes = int(
        os.environ.get(
            "RISK_FLAGS_LOOKBACK_MINUTES",
            DEFAULT_RISK_FLAGS_LOOKBACK_MINUTES,
        )
    )
    decisions_lookback_minutes = int(
        os.environ.get(
            "DECISIONS_LOOKBACK_MINUTES",
            DEFAULT_DECISIONS_LOOKBACK_MINUTES,
        )
    )
    triage_sa_email = os.environ.get(
        "TRIAGE_SA_EMAIL",
        f"asb-agent-triage-sa@{project_id}.iam.gserviceaccount.com",
    ).strip()
    gmail_recipient = os.environ.get("GMAIL_DRAFT_RECIPIENT", DEFAULT_GMAIL_RECIPIENT).strip()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    # ---- BQ client -----------------------------------------------------
    bq_real = BigQueryFanoutClient(project_id=project_id)
    bq: BQQueryClient = _DryRunBQ(bq_real) if dry_run else bq_real

    # ---- Channel adapters ---------------------------------------------
    channel_clients: dict[Channel, ChannelClient] = {}

    if secret_id:
        if dry_run:
            chat_raw: Any = _DryRunChatClient()
        else:
            url = load_webhook_url(
                project_id=project_id,
                secret_id=secret_id,
                version=secret_version,
            )
            chat_raw = ChatWebhookClient(url, http=UrllibPoster())
        channel_clients[Channel.GOOGLE_CHAT_DM] = make_chat_adapter(chat_raw)
    else:
        log.warning("CHAT_WEBHOOK_SECRET_ID is empty — Chat channel disabled.")

    if triage_sa_email and gmail_recipient:
        gmail_raw: Any
        if dry_run:
            gmail_raw = _DryRunGmailClient()
        else:
            from ..common.dwd import DWDServiceFactory
            from .channels.gmail import GMAIL_COMPOSE_SCOPE

            gmail_raw = GmailDraftClient(
                service_factory=DWDServiceFactory(
                    target_principal=triage_sa_email,
                    scope=GMAIL_COMPOSE_SCOPE,
                    api="gmail",
                    api_version="v1",
                )
            )
        channel_clients[Channel.GMAIL_DRAFT] = make_gmail_adapter(
            gmail_raw, recipient_email=gmail_recipient
        )
    else:
        log.warning(
            "Gmail draft channel disabled: triage_sa_email=%r recipient=%r",
            triage_sa_email,
            gmail_recipient,
        )

    if not channel_clients:
        log.error("no channel adapters configured — exiting without polling")
        return 0

    # ---- Audit + agent -------------------------------------------------
    audit = AuditLogClient(project_id=project_id)
    agent = RoutingFanoutAgent(
        sa_email=sa_email,
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        channel_clients=channel_clients,
        bq=bq,
    )

    # ---- Poll + dispatch -----------------------------------------------
    active_channel_keys = tuple(c.value for c in ACTIVE_CHANNELS)

    log.info(
        "fanout tick start: project=%s triaged_lookback=%dm "
        "risk_flags_lookback=%dm decisions_lookback=%dm dry_run=%s channels=%s",
        project_id,
        lookback_minutes,
        risk_flags_lookback_minutes,
        decisions_lookback_minutes,
        dry_run,
        sorted(c.value for c in channel_clients),
    )

    # Triaged items pass.
    triaged_sql = build_triaged_items_poll_query(
        project_id=project_id,
        lookback_minutes=lookback_minutes,
        severities=("critical", "high"),
        channels_to_check=active_channel_keys,
    )
    triaged_result = run_fanout_tick(
        bq=bq, agent=agent, poll_sql=triaged_sql, row_to_input=row_to_input
    )
    log.info(
        "triaged_items tick: polled=%d dispatched=%d skipped=%d "
        "transient_errors=%d permanent_errors=%d "
        "dispatched_per_channel=%s skipped_per_channel=%s",
        triaged_result.polled,
        triaged_result.dispatched,
        triaged_result.skipped,
        len(triaged_result.transient_errors),
        len(triaged_result.permanent_errors),
        dict(sorted(triaged_result.dispatched_per_channel.items())),
        dict(sorted(triaged_result.skipped_per_channel.items())),
    )

    # Risk flags pass (ADR 0033 PR-C). Same agent, same channel
    # adapters, different polling source.
    risk_sql = build_risk_flags_poll_query(
        project_id=project_id,
        lookback_minutes=risk_flags_lookback_minutes,
        severities=("critical", "high"),
        channels_to_check=active_channel_keys,
    )
    risk_result = run_fanout_tick(
        bq=bq, agent=agent, poll_sql=risk_sql, row_to_input=risk_flag_row_to_input
    )
    log.info(
        "risk_flags tick: polled=%d dispatched=%d skipped=%d "
        "transient_errors=%d permanent_errors=%d "
        "dispatched_per_channel=%s skipped_per_channel=%s",
        risk_result.polled,
        risk_result.dispatched,
        risk_result.skipped,
        len(risk_result.transient_errors),
        len(risk_result.permanent_errors),
        dict(sorted(risk_result.dispatched_per_channel.items())),
        dict(sorted(risk_result.skipped_per_channel.items())),
    )

    # Decisions pass (ADR 0041). Same agent + channel adapters; status
    # filter is baked into the polling SQL (status='draft' only). The
    # row converter synthesizes severity='high' so the existing Chat
    # 09:00–16:00 PT window applies — drafts created at 9pm via
    # Reflection v2 surface in Chat the next morning, while Gmail
    # drafts always fire and are available in the inbox immediately.
    decisions_sql = build_decisions_poll_query(
        project_id=project_id,
        lookback_minutes=decisions_lookback_minutes,
        channels_to_check=active_channel_keys,
    )
    decisions_result = run_fanout_tick(
        bq=bq,
        agent=agent,
        poll_sql=decisions_sql,
        row_to_input=make_decision_row_to_input(project_id=project_id),
    )
    log.info(
        "decisions tick: polled=%d dispatched=%d skipped=%d "
        "transient_errors=%d permanent_errors=%d "
        "dispatched_per_channel=%s skipped_per_channel=%s",
        decisions_result.polled,
        decisions_result.dispatched,
        decisions_result.skipped,
        len(decisions_result.transient_errors),
        len(decisions_result.permanent_errors),
        dict(sorted(decisions_result.dispatched_per_channel.items())),
        dict(sorted(decisions_result.skipped_per_channel.items())),
    )

    for err in (
        *triaged_result.transient_errors,
        *risk_result.transient_errors,
        *decisions_result.transient_errors,
    ):
        log.warning("transient error: %s", err)
    for err in (
        *triaged_result.permanent_errors,
        *risk_result.permanent_errors,
        *decisions_result.permanent_errors,
    ):
        log.error("permanent error: %s", err)

    # Exit nonzero on transient errors so the Cloud Run Job execution
    # surfaces as failed and Cloud Scheduler logs it. Permanent errors
    # alone exit 0 — re-running won't help; the audit + permanent log
    # rows are the alert path.
    transient_total = (
        len(triaged_result.transient_errors)
        + len(risk_result.transient_errors)
        + len(decisions_result.transient_errors)
    )
    return 1 if transient_total else 0


if __name__ == "__main__":
    sys.exit(main())
