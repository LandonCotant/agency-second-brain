"""Polling query builders for WS-D routing.

`build_triaged_items_poll_query` returns rows the orchestrator needs to
consider; the optional ``exclude_channel``, ``channels_to_check``, and
``severities`` arguments let the routing fan-out (ADR 0023, ADR 0025,
ADR 0032) and future channel workers narrow the scan without each one
duplicating the SELECT clause.

`build_risk_flags_poll_query` (ADR 0033 PR-C) is the parallel builder
for `agent_outputs.risk_flags`. Risk-Watcher-emitted flags fan out via
the same ``RoutingFanoutAgent`` and ``routed_events`` LEFT JOIN dedup;
``flag_id`` is written into ``routed_events.item_id`` (which is a
generic source-id column) so one table tracks dispatches across both
sources.

`build_decisions_poll_query` (ADR 0041) is the parallel builder for
``agent_outputs.decisions WHERE status='draft'``. Drafts are written
by Captures Materializer (ADR 0039) and Evening Reflection v2 (ADR
0040); the routing fan-out surfaces them for refinement so the user
can fill in alternatives + prediction + confidence and flip status to
``pending``. ``decision_id`` is written into ``routed_events.item_id``,
same generic-source-id pattern as risk_flags.

The queries stay pure-function string builders so the unit tests in
`tests/unit/routing/` can assert on the SQL deterministically.
"""

from __future__ import annotations

from collections.abc import Sequence

_ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})

# The dedup lookback (how far back we check routed_events to avoid
# re-dispatching an already-sent item) is kept WIDE and decoupled from the
# poll lookback. If the two shared a window and an operator widened the poll
# lookback for a backfill, items dispatched before the widening — but inside
# the new poll window — would fall outside the dedup window and re-dispatch.
# The dedup window is always max(poll lookback, this floor) so it can only
# ever be wider than the poll window, never narrower. 7 days comfortably
# covers the daily Risk Watcher cadence plus recovery headroom.
_DEDUP_LOOKBACK_FLOOR_MINUTES = 7 * 24 * 60

# Channel names recorded in ``agent_outputs.routed_events`` (ADR 0025).
# Keep in sync with `routing.matrix.Channel` values; we accept strings
# (not the enum) to keep the SQL builder dependency-free.
_ALLOWED_CHANNELS = frozenset(
    {
        "gemini_inbox",
        "google_chat_dm",
        "gmail_draft",
        "morning_brief",
        "rest_of_queue",
        "airtable_view",
    }
)


def build_triaged_items_poll_query(
    *,
    project_id: str,
    lookback_minutes: int = 5,
    limit: int = 500,
    severities: Sequence[str] | None = None,
    exclude_channel: str | None = None,
    channels_to_check: Sequence[str] | None = None,
) -> str:
    """Return a parameter-free SELECT for the polling worker.

    Args:
      project_id: BQ project id holding `agent_outputs.triaged_items`.
      lookback_minutes: only return rows triaged in the last N minutes.
      limit: cap on rows returned per call.
      severities: optional whitelist (e.g. ``("critical", "high")``).
        ``None`` means no severity filter.
      exclude_channel: single-channel dedup mode. If set, return only
        rows that have NOT yet been recorded in
        ``agent_outputs.routed_events`` for this channel. Value must be
        one of the `routing.matrix.Channel` strings. Mutually exclusive
        with ``channels_to_check``.
      channels_to_check: multi-channel dedup mode (ADR 0032). If set,
        return all rows in the lookback window plus a
        ``routed_channels`` ARRAY<STRING> column listing channels
        already dispatched for that ``item_id``. The agent uses this to
        skip per-channel re-dispatch. Values must be in the channel
        allowlist. Mutually exclusive with ``exclude_channel``.
    """
    if lookback_minutes <= 0:
        raise ValueError("lookback_minutes must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if exclude_channel is not None and channels_to_check is not None:
        raise ValueError("exclude_channel and channels_to_check are mutually exclusive")
    dedup_lookback_minutes = max(lookback_minutes, _DEDUP_LOOKBACK_FLOOR_MINUTES)

    severity_clause = ""
    if severities is not None:
        if not severities:
            raise ValueError("severities must be non-empty if provided")
        invalid = sorted(set(severities) - _ALLOWED_SEVERITIES)
        if invalid:
            raise ValueError(f"unknown severities: {invalid}")
        # Deterministic ordering keeps the SQL stable for snapshot tests.
        joined = ", ".join(f"'{s}'" for s in sorted(set(severities)))
        severity_clause = f"\n  AND ti.severity IN ({joined})"

    exclude_join = ""
    exclude_where = ""
    if exclude_channel is not None:
        if exclude_channel not in _ALLOWED_CHANNELS:
            raise ValueError(f"unknown channel: {exclude_channel!r}")
        # LEFT JOIN against routed_events filtered to this channel; the
        # WHERE clause keeps only rows with no matching event (i.e.
        # not yet dispatched on this channel). routed_events is
        # clustered on item_id, partition-pruned on routed_at — same
        # lookback window keeps the join cheap.
        events_table = f"`{project_id}.agent_outputs.routed_events`"
        exclude_join = (
            f"\nLEFT JOIN {events_table} re\n"
            "  ON re.item_id = ti.item_id\n"
            f"  AND re.channel = '{exclude_channel}'\n"
            "  AND re.routed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
            f"INTERVAL {dedup_lookback_minutes} MINUTE)"
        )
        exclude_where = "\n  AND re.item_id IS NULL"

    routed_channels_select = ""
    if channels_to_check is not None:
        if not channels_to_check:
            raise ValueError("channels_to_check must be non-empty if provided")
        invalid_ch = sorted(set(channels_to_check) - _ALLOWED_CHANNELS)
        if invalid_ch:
            raise ValueError(f"unknown channels: {invalid_ch}")
        # Deterministic ordering keeps the SQL stable for snapshot tests.
        joined_ch = ", ".join(f"'{c}'" for c in sorted(set(channels_to_check)))
        events_table = f"`{project_id}.agent_outputs.routed_events`"
        # Correlated subquery: returns the distinct channels already
        # dispatched for this item_id within the lookback window. The
        # agent uses this set to skip per-channel re-dispatch.
        routed_channels_select = (
            ",\n  ARRAY(\n"
            "    SELECT DISTINCT re.channel\n"
            f"    FROM {events_table} re\n"
            "    WHERE re.item_id = ti.item_id\n"
            f"      AND re.channel IN ({joined_ch})\n"
            "      AND re.routed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
            f"INTERVAL {dedup_lookback_minutes} MINUTE)\n"
            "  ) AS routed_channels"
        )

    triaged_table = f"`{project_id}.agent_outputs.triaged_items`"
    # All interpolated values are validated above (project_id is operator
    # config, lookback_minutes/limit are ints, severities/channel are
    # whitelist-checked). The string-build heuristic that flags
    # f-string SQL doesn't see that here.
    sql = (
        "SELECT\n"
        "  ti.item_id,\n"
        "  ti.triaged_at,\n"
        "  ti.source,\n"
        "  ti.source_url,\n"
        "  ti.source_event_ref,\n"
        "  ti.actionable,\n"
        "  ti.owner_type,\n"
        "  ti.owner_email,\n"
        "  ti.action_type,\n"
        "  ti.severity,\n"
        "  ti.confidence,\n"
        "  ti.human_review_routed,\n"
        "  ti.reasoning,\n"
        "  ti.airtable_task_record_id"
        f"{routed_channels_select}\n"
        f"FROM {triaged_table} ti"
        f"{exclude_join}\n"
        f"WHERE ti.triaged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {lookback_minutes} MINUTE)"
        f"{severity_clause}{exclude_where}\n"
        "ORDER BY ti.triaged_at ASC\n"
        f"LIMIT {limit}"
    )
    return sql


def build_risk_flags_poll_query(
    *,
    project_id: str,
    lookback_minutes: int = 1440,
    limit: int = 500,
    severities: Sequence[str] | None = None,
    channels_to_check: Sequence[str] | None = None,
) -> str:
    """Polling SQL for ``agent_outputs.risk_flags`` (ADR 0033 PR-C).

    Parallels :func:`build_triaged_items_poll_query` but reads from
    ``risk_flags`` and LEFT JOINs ``airtable_replica.accounts`` to
    enrich the row with ``account_name`` for the Gmail-draft subject
    formatter.

    The default ``lookback_minutes`` is 24 hours (vs 5 for triaged
    items) because Risk Watcher fires once a day; a long lookback
    gives the routing fan-out (every 5 min) plenty of recovery
    headroom for transient errors and respects the Chat severity
    window — a flag fired at 06:00 PT is dispatched to Chat once the
    09:00 PT window opens, even if multiple intermediate ticks
    failed.

    Per-channel dedup keys ``routed_events.item_id`` against
    ``flag_id`` (the routed_events column is a generic source-id;
    no DDL change). ``channels_to_check`` defaults to the same
    ACTIVE_CHANNELS the triaged_items poll uses; a flag dispatched
    on Chat in tick N is skipped on Chat in tick N+1, but Gmail
    still dispatches in N+1 if it didn't in N.

    Args:
      project_id: BQ project id holding ``agent_outputs.risk_flags``.
      lookback_minutes: only return flags written in the last N minutes
        (default 1440 = 24h).
      limit: cap on rows returned per call.
      severities: optional whitelist (default ``("critical", "high")``
        when not supplied — those are the only severities the
        routing fan-out's matrix dispatches today).
      channels_to_check: multi-channel dedup mode. If set, return all
        rows in the lookback window plus a ``routed_channels``
        ARRAY<STRING> column listing channels already dispatched for
        that ``flag_id``. Values must be in the channel allowlist.
    """
    if lookback_minutes <= 0:
        raise ValueError("lookback_minutes must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")
    dedup_lookback_minutes = max(lookback_minutes, _DEDUP_LOOKBACK_FLOOR_MINUTES)

    severity_clause = ""
    if severities is not None:
        if not severities:
            raise ValueError("severities must be non-empty if provided")
        invalid = sorted(set(severities) - _ALLOWED_SEVERITIES)
        if invalid:
            raise ValueError(f"unknown severities: {invalid}")
        joined = ", ".join(f"'{s}'" for s in sorted(set(severities)))
        severity_clause = f"\n  AND rf.severity IN ({joined})"

    routed_channels_select = ""
    if channels_to_check is not None:
        if not channels_to_check:
            raise ValueError("channels_to_check must be non-empty if provided")
        invalid_ch = sorted(set(channels_to_check) - _ALLOWED_CHANNELS)
        if invalid_ch:
            raise ValueError(f"unknown channels: {invalid_ch}")
        joined_ch = ", ".join(f"'{c}'" for c in sorted(set(channels_to_check)))
        events_table = f"`{project_id}.agent_outputs.routed_events`"
        # routed_events.item_id is a generic source-id column. Risk
        # Watcher-sourced rows write their flag_id into it.
        routed_channels_select = (
            ",\n  ARRAY(\n"
            "    SELECT DISTINCT re.channel\n"
            f"    FROM {events_table} re\n"
            "    WHERE re.item_id = rf.flag_id\n"
            f"      AND re.channel IN ({joined_ch})\n"
            "      AND re.routed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
            f"INTERVAL {dedup_lookback_minutes} MINUTE)\n"
            "  ) AS routed_channels"
        )

    flags_table = f"`{project_id}.agent_outputs.risk_flags`"
    accounts_table = f"`{project_id}.airtable_replica.accounts`"
    sql = (
        "SELECT\n"
        "  rf.flag_id,\n"
        "  rf.flagged_at,\n"
        "  rf.account_id,\n"
        "  rf.project_id,\n"
        "  rf.segment,\n"
        "  rf.pattern_name,\n"
        "  rf.severity,\n"
        "  rf.signal_evidence,\n"
        "  rf.reasoning,\n"
        "  rf.confidence,\n"
        "  rf.human_review_routed,\n"
        "  rf.airtable_task_record_id,\n"
        "  a.company_name AS account_name"
        f"{routed_channels_select}\n"
        f"FROM {flags_table} rf\n"
        f"LEFT JOIN {accounts_table} a\n"
        "  ON a._airtable_record_id = rf.account_id\n"
        "WHERE rf.flagged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {lookback_minutes} MINUTE)\n"
        # ADR 0035 §5: resolved flags don't dispatch. Lets manual or
        # future agent-side resolution suppress fan-out without DDL.
        "  AND rf.resolved_at IS NULL"
        f"{severity_clause}\n"
        "ORDER BY rf.flagged_at ASC\n"
        f"LIMIT {limit}"
    )
    return sql


def build_decisions_poll_query(
    *,
    project_id: str,
    lookback_minutes: int = 1440,
    limit: int = 500,
    channels_to_check: Sequence[str] | None = None,
) -> str:
    """Polling SQL for ``agent_outputs.decisions WHERE status='draft'`` (ADR 0041).

    Parallels :func:`build_risk_flags_poll_query` but reads from
    ``decisions``. Drafts are written by Captures Materializer (ADR
    0039) and Evening Reflection v2 (ADR 0040); both leave
    ``alternatives=[]``, ``prediction=NULL``, ``confidence=NULL`` for
    the user to refine. The routing fan-out surfaces them via Chat +
    Gmail draft so the user can fill in missing fields and flip
    ``status`` to ``pending`` (manual UPDATE in v1 — see ADR 0041).

    Decisions have no severity column. The fan-out's row converter
    synthesizes ``severity='high'`` so the existing Chat windowing
    (09:00–16:00 PT, ADR 0023) gives the daily-digest UX without a
    separate Cloud Run Job: a draft created at 9pm via Reflection v2
    sits idle until the next 09:00 PT tick fires the Chat card; the
    Gmail draft (always-fire) is available in the inbox immediately.

    Per-channel dedup keys ``routed_events.item_id`` against
    ``decision_id`` (generic source-id column, no DDL change).

    Default ``lookback_minutes`` is 24 hours — matches risk_flags and
    gives the every-5-min routing tick plenty of recovery headroom.

    Args:
      project_id: BQ project id holding ``agent_outputs.decisions``.
      lookback_minutes: only return drafts written in the last N minutes
        (default 1440 = 24h).
      limit: cap on rows returned per call.
      channels_to_check: multi-channel dedup mode. If set, return all
        rows in the lookback window plus a ``routed_channels``
        ARRAY<STRING> column listing channels already dispatched for
        that ``decision_id``. Values must be in the channel allowlist.
    """
    if lookback_minutes <= 0:
        raise ValueError("lookback_minutes must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")

    routed_channels_select = ""
    if channels_to_check is not None:
        if not channels_to_check:
            raise ValueError("channels_to_check must be non-empty if provided")
        invalid_ch = sorted(set(channels_to_check) - _ALLOWED_CHANNELS)
        if invalid_ch:
            raise ValueError(f"unknown channels: {invalid_ch}")
        joined_ch = ", ".join(f"'{c}'" for c in sorted(set(channels_to_check)))
        events_table = f"`{project_id}.agent_outputs.routed_events`"
        # routed_events.item_id is a generic source-id column. Decisions
        # write their decision_id into it.
        routed_channels_select = (
            ",\n  ARRAY(\n"
            "    SELECT DISTINCT re.channel\n"
            f"    FROM {events_table} re\n"
            "    WHERE re.item_id = d.decision_id\n"
            f"      AND re.channel IN ({joined_ch})\n"
            "      AND re.routed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
            f"INTERVAL {lookback_minutes} MINUTE)\n"
            "  ) AS routed_channels"
        )

    decisions_table = f"`{project_id}.agent_outputs.decisions`"
    sql = (
        "SELECT\n"
        "  d.decision_id,\n"
        "  d.decided_at,\n"
        "  d.title,\n"
        "  d.context,\n"
        "  d.choice,\n"
        "  d.status,\n"
        "  d.source_reflection_id,\n"
        "  d.source_voice_note_id"
        f"{routed_channels_select}\n"
        f"FROM {decisions_table} d\n"
        "WHERE d.status = 'draft'\n"
        "  AND d.decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
        f"INTERVAL {lookback_minutes} MINUTE)\n"
        "ORDER BY d.decided_at ASC\n"
        f"LIMIT {limit}"
    )
    return sql
