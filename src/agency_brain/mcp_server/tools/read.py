"""Read tools for the MCP server.

Four tools, one thick (`brain_ask`) wrapping the embed +
VECTOR_SEARCH retrieval pipeline, three thin (`open_risk_flags`,
`client_summary`, `get_calendar_events`) returning structured rows.
The thin tools encode business logic that's tedious to teach Claude
every session: `open_risk_flags` filters ``WHERE resolved_at IS NULL``,
`client_summary` does the cross-dataset JOIN against airtable_replica,
`get_calendar_events` enumerates calendar_event rows by date range so
time-bound queries don't fall through brain_ask's semantic ranking.
"""

from __future__ import annotations

from typing import Any

from ...common.bq_helpers import exclude_hipaa, filter_recency
from ..clients import embedder, get_config, query_rows


def brain_ask(query: str, max_results: int = 8) -> dict[str, Any]:
    """Semantic search over the agency-specific corpus. Returns chunks; you synthesize.

    USE THIS WHEN the user asks free-text questions about anything the
    Brain has ingested over time. Trigger phrases:
      - "what did <person> say about ..."
      - "find the email/note/meeting where ..."
      - "what do I know about ..."
      - "search my notes for ..."
      - "remind me what we discussed re ..."

    DO NOT USE FOR:
      - "What clients are flagged right now?" → use ``open_risk_flags``.
      - "Tell me about <client name>" → use ``client_summary`` (structured
        cross-dataset briefing; richer than free-text recall).
      - Arbitrary BigQuery SQL → use a generic BigQuery MCP if installed.
      - Reading raw Gmail/Drive/Airtable that ISN'T in the corpus → use
        the dedicated ``mcp__claude_ai_Gmail__*`` /
        ``mcp__claude_ai_Google_Drive__*`` /
        ``mcp__claude_ai_Airtable__*`` connectors instead.

    Corpus contents include: calendar events (personal + work),
    ``secondbrain``-labeled Gmail threads, ingested Drive notes,
    voice-memo extracts, ad-hoc captures from ``capture_note``, daily
    reflection extracts. HIPAA-flagged client material is automatically
    excluded.

    Returns retrieved chunks; the host LLM synthesizes prose. This is
    the ADR 0051 split — the Brain is a retrieval substrate, not a
    chat product.

    Args:
        query: Natural-language search query.
        max_results: Top-K chunks to return (default 8, max 50).

    Returns:
        ``{"chunks": [{"note_id", "filename", "source_url", "scope",
        "note_kind", "excerpt", "similarity", "match_type", "rrf_score"}],
        "embedded": bool}``. Higher ``similarity`` = closer semantic match
        (0.0 unrelated, 1.0 exact). ``match_type`` is "semantic", "keyword",
        or "both" (ADR 0068 hybrid retrieval) — a "keyword" hit has
        similarity 0.0 but matched the query terms exactly, so don't
        discount it.
    """
    from ...agents.knowledge_surfacer.retriever import Retriever

    cfg = get_config()
    retriever = Retriever(
        bq_query=_BQAdapter(),
        embedder=embedder(),
        project_id=cfg.project_id,
        notes_table=cfg.notes_table,
        dataset_id=cfg.notes_dataset,
        top_k=max(1, min(50, max_results)),
        cosine_threshold=cfg.cosine_threshold,
        half_life_days=cfg.half_life_days,
        hybrid=cfg.hybrid_enabled,
        rrf_k=cfg.rrf_k,
    )
    chunks = retriever.retrieve(query=query)
    return {
        "chunks": [
            {
                "note_id": c.note_id,
                "filename": c.filename,
                "source_url": c.source_drive_url,
                "scope": c.scope,
                "note_kind": c.note_kind,
                "excerpt": c.markdown_excerpt,
                "similarity": round(c.similarity, 4),
                # ADR 0068 — why this chunk surfaced: "semantic" (vector),
                # "keyword" (exact lexical match), or "both". A keyword hit
                # has similarity 0.0 but earned its place lexically.
                "match_type": c.match_type,
                "rrf_score": round(c.rrf_score, 6) if c.rrf_score is not None else None,
            }
            for c in chunks
        ],
        "embedded": True,
    }


def open_risk_flags(segment: str | None = None, min_severity: str | None = None) -> dict[str, Any]:
    """Currently-open client risk flags. Returns structured rows ready to summarize.

    USE THIS WHEN the user wants the current state of client risks.
    Trigger phrases:
      - "what's flagged right now?"
      - "any client risks?"
      - "what alerts are open?"
      - "anything to worry about with my clients?"
      - "show me Owner Disengagement / Acknowledgment Gap / Silent After Deliverable signals"
      - "how's <segment> doing?" (with the segment filter)

    DO NOT USE FOR:
      - Historical / resolved flags — this returns only WHERE
        ``resolved_at IS NULL``. Use ``brain_ask`` for free-text
        recall of past risk discussions.
      - "What did Risk Watcher do yesterday?" — that's narrative, use
        ``brain_ask`` against the corpus.
      - Resolving / acknowledging flags — there's no write path here.
        Resolve via Airtable directly (the existing Task drafts route).

    Returns active flags from ``agent_outputs.risk_flags`` joined to
    ``airtable_replica.accounts`` for the human-readable
    ``company_name``. Latest 50 ordered by ``flagged_at DESC``.

    Args:
        segment: Optional filter — one of "E-commerce", "Local Service",
            "Agency Partner", "Personal". Unfiltered if omitted.
        min_severity: Optional severity floor — "low", "medium",
            "high", or "critical". Returns rows at or above this level.

    Returns:
        ``{"flags": [{"flag_id", "account_name", "segment", "severity",
        "pattern_name", "flagged_at", "reason"}]}``. Empty list if
        nothing is currently active.
    """
    cfg = get_config()
    # `risk_flags.account_id` is the Airtable record id (e.g. "recXXXX") —
    # JOIN to `airtable_replica.accounts._airtable_record_id` to resolve
    # the human-readable `company_name`. The "reason" output field comes
    # from `risk_flags.reasoning` (the actual column name; the public
    # API stays as "reason" because it's the natural word).
    # Schema verified against prod 2026-05-14.
    # HIPAA guard MUST live in WHERE, not the JOIN: with a LEFT JOIN the
    # ON-clause filter only nulled out `a.*` and still emitted the rf row
    # (segment + reasoning included) for a HIPAA account. The INNER JOIN +
    # WHERE form fails closed: a flag whose account_id has no replica match
    # (HIPAA accounts never sync — that's the point) is dropped entirely.
    # account_id is REQUIRED on risk_flags, so no account-less flags exist.
    where = ["rf.resolved_at IS NULL", exclude_hipaa("a")]
    params: list[dict[str, Any]] = []
    if segment:
        where.append("rf.segment = @segment")
        params.append({"name": "segment", "type": "STRING", "value": segment})
    if min_severity:
        order_map = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        floor = order_map.get(min_severity.lower())
        if floor is not None:
            allowed = [k for k, v in order_map.items() if v >= floor]
            where.append("LOWER(rf.severity) IN UNNEST(@severities)")
            params.append({"name": "severities", "type": "ARRAY_STRING", "value": allowed})
    sql = (
        f"SELECT rf.flag_id, a.company_name AS account_name, rf.segment, "  # noqa: S608
        f"rf.severity, rf.pattern_name, rf.flagged_at, rf.reasoning "
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.risk_flags` AS rf "
        f"JOIN `{cfg.project_id}.airtable_replica.accounts` AS a "
        f"ON rf.account_id = a._airtable_record_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY rf.flagged_at DESC LIMIT 50"
    )
    rows = query_rows(sql, parameters=params)
    return {
        "flags": [
            {
                "flag_id": r.get("flag_id"),
                "account_name": r.get("account_name"),
                "segment": r.get("segment"),
                "severity": r.get("severity"),
                "pattern_name": r.get("pattern_name"),
                "flagged_at": r["flagged_at"].isoformat() if r.get("flagged_at") else None,
                "reason": r.get("reasoning"),
            }
            for r in rows
        ]
    }


def client_summary(account_name: str) -> dict[str, Any]:
    """One-shot structured briefing on a named client / account.

    USE THIS WHEN the user asks for a structured view of a specific
    client. **Prefer this over ``brain_ask`` for any "who is X" or
    "brief me on Y" question** — ``brain_ask`` returns free-text
    excerpts, this returns the canonical Account row + open risks +
    recent operational activity in one call.
    Trigger phrases:
      - "tell me about <client>"
      - "brief me on <client> before our meeting"
      - "what's the status with <client>?"
      - "who is <client>?"
      - "give me everything on <client>"
      - "before I talk to <client>, what do I know?"

    DO NOT USE FOR:
      - Free-text recall ("what did ClientA say about pricing?") — use
        ``brain_ask`` for that; the excerpts there are richer than the
        recent-activity slice here.
      - A list of all flagged clients across the portfolio — use
        ``open_risk_flags`` (no name argument required).
      - Internal team members or non-client accounts.

    Pulls from ``airtable_replica.accounts`` (the canonical record) +
    cross-references: open ``risk_flags``, last 30 days of
    ``triaged_items`` matching the company name in ``source``, last 30
    days of ``note_kind='calendar_event'`` and ``note_kind='email'``
    rows naming the client in ``markdown_content``. HIPAA-flagged
    clients are filtered out via ``hipaa_excluded = FALSE``.

    Match is case-insensitive substring against
    ``accounts.company_name``. If multiple accounts match, returns all
    (rare — disambiguate by passing a more specific substring).

    Args:
        account_name: Client/account name (case-insensitive substring).
            E.g. "ClientA", "Acme", "ClientC".

    Returns:
        ``{"accounts": [{"name", "segment", "hipaa", "open_risk_flags",
        "recent_triaged_items", "recent_meetings", "recent_emails"}]}``.
        Empty list on no match.
    """
    cfg = get_config()
    # airtable_replica.accounts uses `company_name` (not `name`) and
    # `_airtable_record_id` (the Airtable record id, e.g. "recXXXX")
    # is what risk_flags.account_id JOINs to. Schema verified
    # against prod 2026-05-14.
    sql_accounts = (
        f"SELECT _airtable_record_id AS airtable_id, company_name, segment, "  # noqa: S608
        f"COALESCE(hipaa, FALSE) AS hipaa "
        f"FROM `{cfg.project_id}.airtable_replica.accounts` "
        f"WHERE LOWER(company_name) LIKE @pattern "
        f"AND {exclude_hipaa()} "
        f"LIMIT 5"
    )
    accounts = query_rows(
        sql_accounts,
        parameters=[{"name": "pattern", "type": "STRING", "value": f"%{account_name.lower()}%"}],
    )
    if not accounts:
        return {"accounts": []}

    results = []
    for acct in accounts:
        flags = query_rows(
            f"SELECT flag_id, severity, pattern_name, flagged_at, reasoning "  # noqa: S608
            f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.risk_flags` "
            f"WHERE account_id = @aid AND resolved_at IS NULL "
            f"ORDER BY flagged_at DESC LIMIT 10",
            parameters=[{"name": "aid", "type": "STRING", "value": acct["airtable_id"]}],
        )
        triaged = query_rows(
            f"SELECT item_id, severity, category, triaged_at, reasoning "  # noqa: S608
            f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.triaged_items` "
            f"WHERE account_id = @aid "
            f"AND {filter_recency('triaged_at', 30)} "
            f"ORDER BY triaged_at DESC LIMIT 10",
            parameters=[{"name": "aid", "type": "STRING", "value": acct["airtable_id"]}],
        )
        meetings = query_rows(
            f"SELECT note_id, filename, source_drive_url, created_at "  # noqa: S608
            f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.notes` "
            f"WHERE note_kind = 'calendar_event' "
            f"AND LOWER(markdown_content) LIKE @pattern "
            f"AND {filter_recency('created_at', 30)} "
            f"AND {exclude_hipaa(kind='isolated')} "
            f"ORDER BY created_at DESC LIMIT 10",
            parameters=[
                {
                    "name": "pattern",
                    "type": "STRING",
                    "value": f"%{acct['company_name'].lower()}%",
                }
            ],
        )
        emails = query_rows(
            f"SELECT note_id, filename, source_drive_url, created_at "  # noqa: S608
            f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.notes` "
            f"WHERE note_kind = 'email' "
            f"AND LOWER(markdown_content) LIKE @pattern "
            f"AND {filter_recency('created_at', 30)} "
            f"AND {exclude_hipaa(kind='isolated')} "
            f"ORDER BY created_at DESC LIMIT 10",
            parameters=[
                {
                    "name": "pattern",
                    "type": "STRING",
                    "value": f"%{acct['company_name'].lower()}%",
                }
            ],
        )
        results.append(
            {
                "name": acct["company_name"],
                "segment": acct.get("segment"),
                "hipaa": bool(acct.get("hipaa")),
                "open_risk_flags": [
                    {
                        "flag_id": f.get("flag_id"),
                        "severity": f.get("severity"),
                        "pattern_name": f.get("pattern_name"),
                        "flagged_at": f["flagged_at"].isoformat() if f.get("flagged_at") else None,
                        "reason": f.get("reasoning"),
                    }
                    for f in flags
                ],
                "recent_triaged_items": [
                    {
                        "item_id": t.get("item_id"),
                        "severity": t.get("severity"),
                        "category": t.get("category"),
                        "triaged_at": t["triaged_at"].isoformat() if t.get("triaged_at") else None,
                        "reasoning": t.get("reasoning"),
                    }
                    for t in triaged
                ],
                "recent_meetings": [
                    {
                        "note_id": m.get("note_id"),
                        "filename": m.get("filename"),
                        "source_url": m.get("source_drive_url"),
                        "when": m["created_at"].isoformat() if m.get("created_at") else None,
                    }
                    for m in meetings
                ],
                "recent_emails": [
                    {
                        "note_id": e.get("note_id"),
                        "filename": e.get("filename"),
                        "source_url": e.get("source_drive_url"),
                        "when": e["created_at"].isoformat() if e.get("created_at") else None,
                    }
                    for e in emails
                ],
            }
        )
    return {"accounts": results}


def get_calendar_events(
    start_date: str,
    end_date: str,
    scope: str | None = None,
    include_all_day: bool = True,
) -> dict[str, Any]:
    """Enumerate calendar events in a date window. Deterministic, no semantic ranking.

    USE THIS WHEN the user asks anything time-bound about their
    calendar. Trigger phrases:
      - "what's on my calendar this week/next week/today/tomorrow?"
      - "what meetings do I have <date>?"
      - "anything scheduled <day>?"
      - "what's coming up?"
      - "do I have anything on <date>?"
      - any "when is X" question where X might be a meeting / exam /
        birthday / deadline ingested as a calendar event

    DO NOT USE FOR:
      - Free-text recall like "what did <person> say in our last
        meeting" — that's ``brain_ask`` (semantic).
      - "Who is on my team?" — that's not calendar-scoped.
      - Resolved or past meetings discussed in notes — ``brain_ask``.

    Pairs naturally with ``brain_ask`` in a two-phase pattern: call
    this first for the skeleton of the week (the actual scheduled
    items), then ``brain_ask`` for context (related notes, prior
    discussions, risk flags). Calling ``brain_ask`` alone misses
    time-bound items because semantic scoring de-prioritizes events
    without rich text content (e.g. "Oma's birthday").

    Filters ``agent_outputs.notes`` ``WHERE note_kind = 'calendar_event'``
    on ``event_metadata.start`` in ``[start_date, end_date)``. ISO 8601
    string comparison is chronological — no parsing required. HIPAA
    isolation is enforced at the SQL layer.

    Args:
        start_date: ISO date inclusive lower bound, e.g.
            ``"2026-05-14"``. Matches events at or after this date.
        end_date: ISO date exclusive upper bound, e.g.
            ``"2026-05-21"`` for the week starting 2026-05-14.
        scope: Optional ``"agency"`` or ``"personal"`` filter. Omit for
            both. ``"work"`` is accepted as an alias for ``"agency"``.
        include_all_day: When False, drops all-day events (birthdays,
            anniversaries, multi-day events) — useful for "what
            meetings do I have today" queries. Default True.

    Returns:
        ``{"events": [{"note_id", "title", "start", "end", "location",
        "all_day", "scope", "source_url"}], "total": int,
        "date_range": {"start": str, "end": str}}``. Events are sorted
        chronologically by ``start``.
    """
    cfg = get_config()
    scope_normalized = "agency" if scope == "work" else scope
    # All-day events have date-only `start` ("2026-05-18"); timed
    # events are ISO datetimes containing "T" ("2026-05-14T19:00:00-07:00").
    # STRPOS-based detection so the SQL works regardless of how the
    # calendar ingester chose to serialize (date vs datetime-at-midnight).
    sql = (
        f"SELECT note_id, filename, source_drive_url, scope, "  # noqa: S608
        f"event_metadata.start AS start_iso, "
        f"event_metadata.end AS end_iso, "
        f"event_metadata.location AS location, "
        f"event_metadata.status AS status "
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.notes` "
        f"WHERE note_kind = 'calendar_event' "
        f"AND {exclude_hipaa(kind='isolated')} "
        f"AND event_metadata.start IS NOT NULL "
        f"AND event_metadata.start >= @start_date "
        f"AND event_metadata.start < @end_date "
        f"AND COALESCE(event_metadata.status, 'confirmed') != 'cancelled' "
        f"AND (@scope IS NULL OR scope = @scope) "
        f"AND (@include_all_day OR STRPOS(event_metadata.start, 'T') > 0) "
        f"ORDER BY event_metadata.start ASC LIMIT 200"
    )
    rows = query_rows(
        sql,
        parameters=[
            {"name": "start_date", "type": "STRING", "value": start_date},
            {"name": "end_date", "type": "STRING", "value": end_date},
            {"name": "scope", "type": "STRING", "value": scope_normalized},
            {
                "name": "include_all_day",
                "type": "BOOL",
                "value": bool(include_all_day),
            },
        ],
    )
    events = []
    for r in rows:
        start_iso = str(r.get("start_iso") or "")
        is_all_day = "T" not in start_iso
        events.append(
            {
                "note_id": r.get("note_id"),
                "title": r.get("filename"),
                "start": start_iso,
                "end": r.get("end_iso"),
                "location": r.get("location"),
                "all_day": is_all_day,
                "scope": r.get("scope"),
                "source_url": r.get("source_drive_url"),
            }
        )
    return {
        "events": events,
        "total": len(events),
        "date_range": {"start": start_date, "end": end_date},
    }


def related_notes(
    note_id: str,
    link_types: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Graph-walk: notes directly connected to ``note_id`` via ``notes_links``.

    USE THIS WHEN the user wants to navigate the corpus structurally
    rather than semantically. Trigger phrases:
      - "what's connected to <note>"
      - "what links from <note>"
      - "show me related notes for <note>"
      - "what does <topic> point to" (when a note's note_id is known)
      - graph-traversal flavored questions

    DO NOT USE FOR:
      - Free-text recall — ``brain_ask`` handles "what did I write
        about X" without needing a starting note_id.
      - "What client is this about" — ``client_summary`` is the
        structured client briefing path.
      - Time-bound questions — ``get_calendar_events``.

    Walks ``agent_outputs.notes_links`` edges where
    ``source_note_id = @note_id``. Returns BOTH:

      - ``link_type='semantic'`` (Librarian VECTOR_SEARCH neighbors,
        ADR 0045) — including rows with NULL ``link_type`` from before
        ADR 0053 added the column.
      - ``link_type='wikilink'`` (user-typed ``[[X]]`` from ADR 0053).

    Joins to ``agent_outputs.notes`` to enrich the target side with
    ``filename`` + ``source_drive_url`` so the caller doesn't need to
    re-query. Ordered by similarity DESC so wikilink edges (1.0) and
    high-confidence semantic neighbors land first.

    v1 supports depth=1 only. Recursive traversal is a follow-up
    (needs cycle detection + cost guards).

    Args:
        note_id: The focal note's ``agent_outputs.notes.note_id``.
            Get this from a prior ``brain_ask`` / ``client_summary``
            call's response.
        link_types: Optional filter, e.g. ``["wikilink"]`` to only
            return user-asserted edges. ``None`` (default) returns
            both. Unknown values are passed through; callers can
            extend the enum without code changes.
        limit: Max edges to return (default 20, max 100).

    Returns:
        ``{"links": [{"target_note_id", "target_filename",
        "target_url", "similarity", "link_type"}], "total": int,
        "source_note_id": str}``. Empty list if the note has no
        outgoing edges (common today — corpus is small).
    """
    cfg = get_config()
    limit = max(1, min(100, int(limit)))
    where_clauses = ["nl.source_note_id = @src"]
    params: list[dict[str, Any]] = [
        {"name": "src", "type": "STRING", "value": note_id},
    ]
    if link_types:
        # NULL link_type rows are interpreted as 'semantic' (ADR 0053 §1).
        normalized = [str(lt) for lt in link_types]
        if "semantic" in normalized and "NULL" not in normalized:
            # Asking for 'semantic' should include NULL rows for
            # backward compat with pre-ADR-0053 writes.
            where_clauses.append("(nl.link_type IN UNNEST(@link_types) OR nl.link_type IS NULL)")
        else:
            where_clauses.append("nl.link_type IN UNNEST(@link_types)")
        params.append({"name": "link_types", "type": "ARRAY_STRING", "value": normalized})

    sql = (
        f"SELECT nl.target_note_id, n.filename AS target_filename, "  # noqa: S608
        f"n.source_drive_url AS target_url, nl.similarity, "
        f"COALESCE(nl.link_type, 'semantic') AS link_type "
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.{cfg.links_table}` AS nl "
        f"LEFT JOIN `{cfg.project_id}.{cfg.outputs_dataset}.{cfg.notes_table}` AS n "
        f"ON nl.target_note_id = n.note_id "
        f"AND {exclude_hipaa('n', kind='isolated')} "
        f"WHERE {' AND '.join(where_clauses)} "
        f"ORDER BY nl.similarity DESC, link_type LIMIT @limit"
    )
    params.append({"name": "limit", "type": "INT64", "value": limit})

    rows = query_rows(sql, parameters=params)
    return {
        "source_note_id": note_id,
        "links": [
            {
                "target_note_id": r.get("target_note_id"),
                "target_filename": r.get("target_filename"),
                "target_url": r.get("target_url"),
                "similarity": float(r.get("similarity") or 0.0),
                "link_type": r.get("link_type"),
            }
            for r in rows
        ],
        "total": len(rows),
    }


def open_drafts(limit: int = 20) -> dict[str, Any]:
    """List Airtable Tasks awaiting your approval (status='Drafted by Agent').

    USE THIS WHEN the user asks:
      - "What's queued for me?" / "What needs my approval?"
      - "Show me the open drafts" / "What drafts are waiting?"
      - "Anything to review?" — when the context is approving agent-drafted Tasks.

    DO NOT USE FOR:
      - Free-text recall (use ``brain_ask``).
      - Tasks the user wrote themselves (those don't carry the 'Drafted by
        Agent' approval_status — they're handled in Airtable directly).
      - Risk flags (use ``open_risk_flags`` — different surface).

    Source: ``airtable_replica.tasks WHERE approval_status='Drafted by Agent'``.
    HIPAA-excluded rows are filtered. Sorted by creation time, newest first.

    Args:
        limit: Max rows to return. Default 20, clamped to [1, 100].

    Returns:
        ``{
            "drafts": [
                {
                    "task_id": str,             # Airtable _airtable_record_id
                    "task_name": str,
                    "category": str | None,
                    "action_type": str | None,
                    "task_type": str | None,
                    "owner": str | None,
                    "source": str | None,
                    "source_reference": str | None,
                    "due_date": str | None,     # ISO YYYY-MM-DD
                    "created": str | None,      # ISO timestamp
                }
            ],
            "total": int,
        }``
    """
    cfg = get_config()
    safe_limit = max(1, min(int(limit), 100))
    sql = (
        f"SELECT _airtable_record_id AS task_id, task_name, "  # noqa: S608
        f"category, action_type, task_type, owner, source, source_reference, "
        f"due_date, _airtable_last_modified AS created "
        f"FROM `{cfg.project_id}.airtable_replica.tasks` "
        f"WHERE approval_status = 'Drafted by Agent' "
        f"AND {exclude_hipaa()} "
        f"ORDER BY _airtable_last_modified DESC LIMIT @limit"
    )
    rows = query_rows(
        sql,
        parameters=[{"name": "limit", "type": "INT64", "value": safe_limit}],
    )
    return {
        "drafts": [
            {
                "task_id": r.get("task_id"),
                "task_name": r.get("task_name"),
                "category": r.get("category"),
                "action_type": r.get("action_type"),
                "task_type": r.get("task_type"),
                "owner": r.get("owner"),
                "source": r.get("source"),
                "source_reference": r.get("source_reference"),
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "created": r["created"].isoformat() if r.get("created") else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


def open_commitments(
    direction: str | None = None,
    account_name: str | None = None,
    days_overdue: int = 0,
) -> dict[str, Any]:
    """Open commitments that are due/overdue — extracted action memory (ADR 0069).

    USE THIS WHEN the user asks what they promised and haven't done, or who's
    behind on what they owed. Trigger phrases:
      - "what did I say I'd do / what am I behind on / what's overdue"
      - "what's <person/client> supposed to send me"
      - "anything I promised that I haven't followed through on"

    This is the *extracted* counterpart to ``pending_followups`` (which reads
    the manually-set Airtable next_followup). Commitments are mined from the
    corpus (emails, voice memos, calendar, captures) by the daily extractor.

    Args:
        direction: ``"mine"`` (what the operator promised) | ``"theirs"`` (what
            others promised the operator) | None (both).
        account_name: optional case-insensitive substring filter on the
            resolved account.
        days_overdue: 0 = due today or already overdue; 7 = at least 7 days
            past due. A commitment with no explicit due date goes overdue
            ``COMMITMENT_STALE_DAYS`` (default 7) after extraction.

    Returns:
        ``{"commitments": [{"commitment_id", "direction", "counterparty",
        "account", "what", "due_date", "effective_due", "days_overdue",
        "source_url", "confidence"}]}`` — most overdue first.
    """
    cfg = get_config()
    eff_due = f"COALESCE(c.due_date, DATE(c.extracted_at) + " f"{int(cfg.commitment_stale_days)})"
    where = [
        "c.status = 'open'",
        exclude_hipaa("n", kind="isolated"),
        exclude_hipaa("a"),  # drop HIPAA-account commitments (null acct passes)
        f"{eff_due} <= DATE_SUB(CURRENT_DATE(), INTERVAL @days_overdue DAY)",
    ]
    params: list[dict[str, Any]] = [
        {"name": "days_overdue", "type": "INT64", "value": max(0, int(days_overdue))},
    ]
    if direction in ("mine", "theirs"):
        where.append("c.direction = @direction")
        params.append({"name": "direction", "type": "STRING", "value": direction})
    if account_name:
        where.append("LOWER(a.company_name) LIKE @acct")
        params.append({"name": "acct", "type": "STRING", "value": f"%{account_name.lower()}%"})
    sql = (
        f"SELECT c.commitment_id, c.direction, "  # noqa: S608
        f"COALESCE(c.counterparty_name, c.counterparty_email) AS counterparty, "
        f"a.company_name AS account_name, c.commitment_text, c.due_date, "
        f"{eff_due} AS effective_due, "
        f"DATE_DIFF(CURRENT_DATE(), {eff_due}, DAY) AS days_overdue, "
        f"n.source_drive_url AS source_url, c.confidence "
        f"FROM `{cfg.project_id}.{cfg.outputs_dataset}.commitments` AS c "
        f"JOIN `{cfg.project_id}.{cfg.outputs_dataset}.notes` AS n "
        f"ON c.source_note_id = n.note_id "
        f"LEFT JOIN `{cfg.project_id}.airtable_replica.accounts` AS a "
        f"ON c.account_id = a._airtable_record_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY effective_due ASC LIMIT 50"
    )
    rows = query_rows(sql, parameters=params)
    return {
        "commitments": [
            {
                "commitment_id": r.get("commitment_id"),
                "direction": r.get("direction"),
                "counterparty": r.get("counterparty"),
                "account": r.get("account_name"),
                "what": r.get("commitment_text"),
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "effective_due": (
                    r["effective_due"].isoformat() if r.get("effective_due") else None
                ),
                "days_overdue": r.get("days_overdue"),
                "source_url": r.get("source_url"),
                "confidence": (
                    round(float(r["confidence"]), 3) if r.get("confidence") is not None else None
                ),
            }
            for r in rows
        ]
    }


def entity_facts(entity_name: str, include_history: bool = False) -> dict[str, Any]:
    """Current attribute facts about a client/person — bi-temporal memory (ADR 0070).

    USE THIS WHEN the user wants the current state of a known entity's
    attributes — "what's Acme's retainer", "is Tim the owner now", "what's
    the status on WeCare" — or how an attribute changed over time. Facts are
    extracted from the corpus and carry a confidence + a source link; they're
    advisory, not authoritative — verify against the source for anything that
    matters.

    DO NOT USE FOR:
      - Free-text recall of what was discussed → ``brain_ask``.
      - A full client briefing → ``client_summary`` (this is just attributes).
      - Open promises / follow-ups → ``open_commitments`` / ``pending_followups``.

    Validity is derived at read time (ADR 0070 §3): for each
    ``(entity, predicate)`` the row with the latest ``observed_date`` is
    current (``valid_to`` is null); older rows are superseded but retained.

    Args:
        entity_name: account or person name (case-insensitive substring).
        include_history: if True, also return superseded values with the
            date each stopped being current.

    Returns:
        ``{"facts": [{"entity", "predicate", "value", "since", "valid_to",
        "current", "confidence", "source_url"}]}`` — current first.
    """
    cfg = get_config()
    where = [
        "LOWER(f.entity_name) LIKE @q",
        exclude_hipaa("n", kind="isolated"),
        exclude_hipaa("a"),  # drop HIPAA-account facts (null acct passes)
    ]
    params: list[dict[str, Any]] = [
        {"name": "q", "type": "STRING", "value": f"%{entity_name.lower()}%"},
    ]
    outer = "" if include_history else "WHERE valid_to IS NULL"
    sql = (
        f"WITH base AS ("  # noqa: S608
        f"  SELECT f.entity_name, f.predicate, f.value, f.observed_date, "
        f"  f.confidence, n.source_drive_url AS source_url, "
        f"  LEAD(f.observed_date) OVER ("
        f"    PARTITION BY COALESCE(f.entity_id, LOWER(f.entity_name)), f.predicate "
        f"    ORDER BY f.observed_date ASC, f.extracted_at ASC) AS valid_to "
        f"  FROM `{cfg.project_id}.{cfg.outputs_dataset}.facts` AS f "
        f"  JOIN `{cfg.project_id}.{cfg.outputs_dataset}.notes` AS n "
        f"  ON f.source_note_id = n.note_id "
        f"  LEFT JOIN `{cfg.project_id}.airtable_replica.accounts` AS a "
        f"  ON f.entity_id = a._airtable_record_id "
        f"  WHERE {' AND '.join(where)}"
        f") "
        f"SELECT entity_name, predicate, value, observed_date, valid_to, "
        f"confidence, source_url FROM base "
        f"{outer} "
        f"ORDER BY entity_name, predicate, observed_date DESC LIMIT 100"
    )
    rows = query_rows(sql, parameters=params)
    return {
        "facts": [
            {
                "entity": r.get("entity_name"),
                "predicate": r.get("predicate"),
                "value": r.get("value"),
                "since": (r["observed_date"].isoformat() if r.get("observed_date") else None),
                "valid_to": (r["valid_to"].isoformat() if r.get("valid_to") else None),
                "current": r.get("valid_to") is None,
                "confidence": (
                    round(float(r["confidence"]), 3) if r.get("confidence") is not None else None
                ),
                "source_url": r.get("source_url"),
            }
            for r in rows
        ]
    }


class _BQAdapter:
    """Tiny adapter so the Retriever's BQQueryClient protocol is satisfied."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        return query_rows(sql, parameters=parameters)
