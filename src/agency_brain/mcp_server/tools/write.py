"""Write tools for the MCP server.

Four tools, each with a safety wrapper that prevents the LLM from
doing damage if a prompt-injected input drives a tool call:

  - ``capture_note``: hash-keyed MERGE so duplicate captures collapse;
    forces ``scope='personal'`` and ``note_kind='capture'`` so writes
    can't masquerade as another corpus class.
  - ``mark_decision_status``: ``WHERE status = 'drafted'`` guard so
    confirmed/dismissed decisions cannot be re-toggled.
  - ``insert_decision``: forces ``status='drafted'``; no way to insert
    a directly-confirmed decision (must transition via
    ``mark_decision_status``). Also writes a synthetic notes row
    (ADR 0052) so ``brain_ask`` finds the decision.
  - ``insert_win``: ``title_hash12`` dedup (matches Brag Spotter's
    cross-agent pattern from ADR 0043 §3). Also writes a synthetic
    notes row (ADR 0052) for ``brain_ask`` retrievability.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ..clients import embedder, get_config, query_rows

log = logging.getLogger("agency_brain.mcp_server.tools.write")


def _insert_synthetic_note(
    *,
    note_id: str,
    note_kind: str,
    source_record_id: str,
    title: str,
    body: str,
    revision_id: str,
    extraction_method: str,
) -> tuple[bool, str | None]:
    """Write a companion notes row representing a decision/win/etc. (ADR 0052).

    Returns ``(inserted, error)``. Best-effort: failures here log and
    return ``(False, "...")`` so the caller can record the partial-
    success state on the canonical row write, but never crash the
    parent tool. The canonical ``decisions`` / ``wins`` row is the
    source of truth; the synthetic note is a retrieval index.

    Idempotent on ``note_id`` — if a row already exists, returns
    ``(False, None)`` without re-inserting or re-embedding.
    """
    cfg = get_config()

    # Dedup pre-check on note_id alone — it's the unique key. Adding
    # note_kind would make a note_id collision across kinds slip past the
    # dedup and write a duplicate-keyed row.
    existing = query_rows(
        f"SELECT note_id FROM `{cfg.project_id}.{cfg.outputs_dataset}.notes` "  # noqa: S608
        f"WHERE note_id = @nid LIMIT 1",
        parameters=[
            {"name": "nid", "type": "STRING", "value": note_id},
        ],
    )
    if existing:
        return (False, None)

    markdown_content = f"# {title}\n\n{body}".strip()
    content_hash = hashlib.sha256(markdown_content.encode()).hexdigest()
    try:
        embedding = list(embedder().embed(text=markdown_content, model="text-embedding-005"))
    except Exception as exc:
        log.warning(
            "synthetic_note.embed_failed note_id=%s kind=%s err=%s",
            note_id,
            note_kind,
            exc,
        )
        return (False, f"embed_failed: {exc}")

    sql = (
        f"INSERT INTO `{cfg.project_id}.{cfg.outputs_dataset}.notes` "  # noqa: S608
        f"(note_id, revision_id, ingested_at, created_at, "
        f"source_drive_file_id, source_drive_url, filename, "
        f"extraction_method, extraction_confidence, page_count, "
        f"hipaa_isolated, note_kind, scope, markdown_content, "
        f"external_id, embedding, embedding_model, embedding_content_hash) "
        f"VALUES "
        f"(@note_id, @revision_id, @now, @now, @source_drive_file_id, '', "
        f"@filename, @extraction_method, 1.0, 1, FALSE, @note_kind, 'personal', "
        f"@text, @external_id, @embedding, 'text-embedding-005', @content_hash)"
    )
    query_rows(
        sql,
        parameters=[
            {"name": "note_id", "type": "STRING", "value": note_id},
            {"name": "revision_id", "type": "STRING", "value": revision_id},
            {"name": "now", "type": "TIMESTAMP", "value": datetime.now(UTC).isoformat()},
            {
                "name": "source_drive_file_id",
                "type": "STRING",
                "value": f"{note_kind}:{source_record_id}",
            },
            {"name": "filename", "type": "STRING", "value": title[:120]},
            {"name": "extraction_method", "type": "STRING", "value": extraction_method},
            {"name": "note_kind", "type": "STRING", "value": note_kind},
            {"name": "text", "type": "STRING", "value": markdown_content},
            {"name": "external_id", "type": "STRING", "value": source_record_id},
            {"name": "embedding", "type": "ARRAY_FLOAT64", "value": embedding},
            {"name": "content_hash", "type": "STRING", "value": content_hash},
        ],
    )

    # ADR 0053 §4 — parse [[X]] in the synthesized markdown (title +
    # body) and materialize wikilink edges. Best-effort; failures here
    # log and continue.
    cfg = get_config()
    from ...common.wikilink_parser import write_wikilink_edges

    try:
        write_wikilink_edges(
            source_note_id=note_id,
            markdown_content=markdown_content,
            project_id=cfg.project_id,
            dataset_id=cfg.outputs_dataset,
            notes_table=cfg.notes_table,
            links_table=cfg.links_table,
            query_rows=query_rows,
        )
    except Exception as exc:
        log.warning(
            "synthetic_note.wikilink_write_failed note_id=%s err=%s",
            note_id,
            exc,
        )
    return (True, None)


def capture_note(text: str, source_hint: str | None = None) -> dict[str, Any]:
    """Capture an ad-hoc thought into the corpus so it's retrievable later.

    USE THIS WHEN the user wants something remembered for future
    retrieval. Trigger phrases:
      - "remember that ..."
      - "capture this: ..."
      - "save this for later: ..."
      - "make a note that ..."
      - "don't let me forget ..."
      - "log: ..."
      - "for the brain: ..."

    DO NOT USE FOR:
      - Long-form documents the user is writing — use Anthropic's
        Drive MCP ``mcp__claude_ai_Google_Drive__create_file`` for
        anything ≥ a few paragraphs that deserves its own Doc.
      - Reflection-doc content extracted from voice memos — the
        scheduled Claude routine uses ``insert_decision`` /
        ``insert_win`` for structured outputs, not this.
      - Drafts of emails / tasks / Airtable updates — use the
        appropriate connector instead; this tool only writes to the
        searchable corpus, not to operational systems.

    Embeds ``text`` via ``text-embedding-005`` and INSERTs into
    ``agent_outputs.notes`` with ``note_kind='capture'``,
    ``scope='personal'``, ``hipaa_isolated=False``. Idempotent on
    SHA-256 of the trimmed text — calling twice with the same content
    returns ``duplicate=true`` without re-inserting.

    Once captured, the note becomes retrievable via ``brain_ask``
    (semantic search) within seconds — no re-indexing step.

    Args:
        text: The note body (whatever the user wants remembered).
        source_hint: Optional provenance label, e.g.
            ``"voice-claude-mobile"`` or ``"chat-2026-05-14"``.
            Stored in ``filename`` for later inspection.

    Returns:
        ``{"note_id", "inserted": bool, "duplicate": bool}``. When
        ``duplicate=true``, the existing row's ``note_id`` is returned.
    """
    text = (text or "").strip()
    if not text:
        return {"note_id": None, "inserted": False, "duplicate": False, "error": "empty text"}

    cfg = get_config()
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    note_id = f"cap-{content_hash[:12]}"
    filename = source_hint or f"capture-{datetime.now(UTC).date().isoformat()}"
    now_iso = datetime.now(UTC).isoformat()

    # Dedup pre-check — keeps the BQ MERGE cheap on retries.
    existing = query_rows(
        f"SELECT note_id FROM `{cfg.project_id}.{cfg.outputs_dataset}.notes` "
        f"WHERE note_id = @nid AND note_kind = 'capture' LIMIT 1",
        parameters=[{"name": "nid", "type": "STRING", "value": note_id}],
    )
    if existing:
        return {"note_id": note_id, "inserted": False, "duplicate": True}

    try:
        embedding = list(embedder().embed(text=text, model="text-embedding-005"))
    except Exception as e:
        return {"note_id": note_id, "inserted": False, "duplicate": False, "error": str(e)}

    # Use parameterized INSERT — no streaming buffer concerns since
    # captures are low-volume + we won't immediately UPDATE the row.
    sql = (
        f"INSERT INTO `{cfg.project_id}.{cfg.outputs_dataset}.notes` "
        f"(note_id, revision_id, ingested_at, created_at, source_drive_file_id, "
        f"source_drive_url, filename, extraction_method, extraction_confidence, "
        f"page_count, hipaa_isolated, note_kind, scope, markdown_content, "
        f"external_id, embedding, embedding_model, embedding_content_hash) "
        f"VALUES "
        f"(@note_id, @revision_id, @now, @now, @source_drive_file_id, '', "
        f"@filename, 'mcp-capture-v1', 1.0, 1, FALSE, 'capture', 'personal', "
        f"@text, @note_id, @embedding, 'text-embedding-005', @content_hash)"
    )
    query_rows(
        sql,
        parameters=[
            {"name": "note_id", "type": "STRING", "value": note_id},
            {"name": "revision_id", "type": "STRING", "value": content_hash},
            {"name": "now", "type": "TIMESTAMP", "value": now_iso},
            {
                "name": "source_drive_file_id",
                "type": "STRING",
                "value": f"capture:{note_id}",
            },
            {"name": "filename", "type": "STRING", "value": filename},
            {"name": "text", "type": "STRING", "value": text},
            {"name": "embedding", "type": "ARRAY_FLOAT64", "value": embedding},
            {"name": "content_hash", "type": "STRING", "value": content_hash},
        ],
    )

    # ADR 0053 — parse [[X]] wikilinks and materialize graph edges.
    # Best-effort: failures here log and continue; the notes row above
    # is the source of truth.
    from ...common.wikilink_parser import write_wikilink_edges

    wikilink_result = write_wikilink_edges(
        source_note_id=note_id,
        markdown_content=text,
        project_id=cfg.project_id,
        dataset_id=cfg.outputs_dataset,
        notes_table=cfg.notes_table,
        links_table=cfg.links_table,
        query_rows=query_rows,
    )
    return {
        "note_id": note_id,
        "inserted": True,
        "duplicate": False,
        "wikilinks": wikilink_result,
    }


def mark_decision_status(decision_id: str, status: str) -> dict[str, Any]:
    """Transition a drafted decision to confirmed or dismissed.

    USE THIS WHEN the user has reviewed an Evening Reflection or
    Captures-Materializer draft and wants to act on it. Trigger
    phrases:
      - "confirm decision <id>"
      - "mark decision <id> as confirmed/dismissed"
      - "approve <decision_id>"
      - "dismiss <decision_id>"
      - "I've decided to go ahead with <decision_id>"
      - "scratch <decision_id>"

    DO NOT USE FOR:
      - Creating a new decision — use ``insert_decision`` instead.
      - Marking tasks complete — those live in Airtable, use the
        Airtable MCP.
      - Transitioning anything outside ``agent_outputs.decisions`` —
        this tool is decision-row-only.

    Drafts-only safety wrapper: the UPDATE has ``WHERE status =
    'drafted'`` so confirmed/dismissed rows cannot be retoggled. If
    you call this on an already-transitioned row, it returns
    ``updated=false`` with the actual ``prior_status`` so the user can
    see the no-op. ``status`` must be exactly ``"confirmed"`` or
    ``"dismissed"`` — any other value returns an error.

    Args:
        decision_id: The decision row to transition (e.g.
            ``"dec-abc123def456"``). Get IDs from
            ``recent_decisions`` (when that read tool ships in v0.2)
            or from the Evening Reflection's Doc artifact.
        status: ``"confirmed"`` (you're going to do it) or
            ``"dismissed"`` (you're not).

    Returns:
        ``{"updated": bool, "prior_status": str | None,
        "new_status": str | None}``. ``updated=false`` either means
        the decision wasn't drafted (already transitioned) or wasn't
        found.
    """
    if status not in ("confirmed", "dismissed"):
        return {
            "updated": False,
            "error": f"status must be 'confirmed' or 'dismissed', got {status!r}",
        }

    cfg = get_config()
    # Read prior status for the response shape (and to give the LLM
    # context on whether the call was a no-op).
    prior_rows = query_rows(
        f"SELECT status FROM `{cfg.project_id}.{cfg.outputs_dataset}.decisions` "
        f"WHERE decision_id = @did LIMIT 1",
        parameters=[{"name": "did", "type": "STRING", "value": decision_id}],
    )
    prior_status = prior_rows[0]["status"] if prior_rows else None
    if prior_status is None:
        return {"updated": False, "prior_status": None, "error": "decision not found"}
    if prior_status != "drafted":
        return {"updated": False, "prior_status": prior_status, "new_status": prior_status}

    # ``refined_at`` is the schema's "row was modified after initial
    # draft" timestamp (see terraform/modules/agent_runtime/main.tf
    # decisions table). Set it alongside status so consumers can
    # distinguish freshly-drafted rows from transitioned ones without
    # joining to the audit log.
    query_rows(
        f"UPDATE `{cfg.project_id}.{cfg.outputs_dataset}.decisions` "
        f"SET status = @status, refined_at = CURRENT_TIMESTAMP() "
        f"WHERE decision_id = @did AND status = 'drafted'",
        parameters=[
            {"name": "did", "type": "STRING", "value": decision_id},
            {"name": "status", "type": "STRING", "value": status},
        ],
    )
    return {"updated": True, "prior_status": "drafted", "new_status": status}


def insert_decision(text: str, source: str | None = None) -> dict[str, Any]:
    """Insert a new drafted decision row (primarily for Claude scheduled routines).

    USE THIS WHEN you're a scheduled Claude routine (Evening Reflection
    REFLECT mode) extracting decisions from voice memos / day-summary
    content. The migrated routine reads voice memos via the Drive MCP,
    extracts structured decisions, and calls this tool to write each
    one.

    PROBABLY DO NOT USE FROM A CHAT CONVERSATION. If the user is
    actively talking to you, prefer:
      - ``capture_note`` for general "remember this" content (writes
        to the corpus, retrievable via brain_ask).
      - A direct conversation with the user about the decision rather
        than serializing it as a drafted row that requires later
        ``mark_decision_status`` follow-up.

    The narrow exception: if the user explicitly says "create a
    decision row for X" or "log this as a decision I need to confirm,"
    use this. Otherwise default to ``capture_note``.

    Drafts-only safety wrapper: always inserts with
    ``status='drafted'`` (PRD §4.7). There's no path to create a
    confirmed decision directly — the human transitions via
    ``mark_decision_status`` after review.

    Args:
        text: The decision content. Markdown OK.
        source: Optional provenance, e.g.
            ``"evening-reflection-2026-05-14"`` or
            ``"manual-claude-chat"``. Defaults to ``"mcp-server"``.

    Returns:
        ``{"decision_id": str, "inserted": bool}``.
    """
    text = (text or "").strip()
    if not text:
        return {"decision_id": None, "inserted": False, "error": "empty text"}

    cfg = get_config()
    decision_id = f"dec-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    # Derive a short title from the first line / first 80 chars. The
    # full text goes into `choice` (the canonical "what was decided"
    # column per ADR 0039). Future structured callers (Evening
    # Reflection v2 routine, Phase 3) will pass title+context+choice
    # separately and supersede this auto-derivation.
    from datetime import timedelta

    first_line = text.splitlines()[0].strip()
    title = first_line[:80] if first_line else text[:80]
    today = now.date()
    sql = (
        f"INSERT INTO `{cfg.project_id}.{cfg.outputs_dataset}.decisions` "  # noqa: S608
        f"(decision_id, decided_at, title, context, choice, status, "
        f"review_30_at, review_90_at, review_365_at, source_reflection_id) "
        f"VALUES (@did, @now, @title, @context, @choice, 'drafted', "
        f"@r30, @r90, @r365, @source)"
    )
    query_rows(
        sql,
        parameters=[
            {"name": "did", "type": "STRING", "value": decision_id},
            {"name": "now", "type": "TIMESTAMP", "value": now_iso},
            {"name": "title", "type": "STRING", "value": title},
            {"name": "context", "type": "STRING", "value": ""},
            {"name": "choice", "type": "STRING", "value": text},
            {
                "name": "r30",
                "type": "DATE",
                "value": (today + timedelta(days=30)).isoformat(),
            },
            {
                "name": "r90",
                "type": "DATE",
                "value": (today + timedelta(days=90)).isoformat(),
            },
            {
                "name": "r365",
                "type": "DATE",
                "value": (today + timedelta(days=365)).isoformat(),
            },
            {"name": "source", "type": "STRING", "value": source or "mcp-server"},
        ],
    )

    # ADR 0052 — synthetic notes row for brain_ask retrievability.
    # Best-effort; embedder failures don't fail the parent decision write.
    synthetic_note_id = f"syn-dec-{decision_id}"
    note_inserted, note_error = _insert_synthetic_note(
        note_id=synthetic_note_id,
        note_kind="decision",
        source_record_id=decision_id,
        title=title,
        body=text,
        revision_id=now_iso,
        extraction_method="synthetic-decision-v1",
    )
    return {
        "decision_id": decision_id,
        "inserted": True,
        "synthetic_note_id": synthetic_note_id if note_inserted else None,
        "synthetic_note_error": note_error,
    }


def insert_win(
    title: str, context: str | None = None, source_id: str | None = None
) -> dict[str, Any]:
    """Insert a new win row (primarily for Claude scheduled routines).

    USE THIS WHEN you're a scheduled Claude routine extracting wins
    from voice memos (Evening Reflection REFLECT) or aggregating a
    week's accomplishments (Brag Spotter migrated routine). Writes to
    ``agent_outputs.wins`` with the same ``title_hash12`` dedup
    pattern as the original Brag Spotter Cloud Run job (ADR 0043 §3),
    so MCP-inserted wins don't collide with Sunday's batch
    aggregation.

    PROBABLY DO NOT USE FROM A CHAT CONVERSATION. If the user
    spontaneously says "I shipped the MCP server!" the better
    response is celebration + ``capture_note`` (semantic recall later)
    rather than writing a structured wins row that only Brag Spotter's
    weekly aggregator queries. Use this only when:
      - You're a scheduled routine following an explicit win-extraction
        prompt, OR
      - The user explicitly says "log this as a win" / "add this to
        my wins" / "track this for Brag Spotter."

    Dedup is case-insensitive on ``title`` (the same hash recipe
    Brag Spotter uses). Calling twice with case variations of the
    same title returns ``duplicate=true`` with the existing ``win_id``.

    Args:
        title: Short title for the win (used for ``title_hash12``
            dedup).
        context: Optional longer-form paragraph.
        source_id: Optional provenance — a related task_id or
            decision_id. Defaults to ``"mcp-server"``.

    Returns:
        ``{"win_id": str, "inserted": bool, "duplicate": bool}``.
    """
    title = (title or "").strip()
    if not title:
        return {"win_id": None, "inserted": False, "duplicate": False, "error": "empty title"}

    cfg = get_config()
    # title_hash12 — same recipe as Brag Spotter's writer
    # (src/agency_brain/agents/brag_spotter/writer.py). Sharing the
    # hash function means an MCP-inserted win with the same title as a
    # Brag Spotter aggregation lands at a *different* win_id (different
    # prefix) — by design: Brag Spotter wins are weekly aggregations,
    # MCP wins are user-driven captures; we keep them distinguishable.
    from datetime import timedelta

    title_hash12 = hashlib.sha256(title.lower().encode()).hexdigest()[:12]
    now = datetime.now(UTC)
    today = now.date()
    # Monday of the current week (ISO week start). Matches Brag Spotter's
    # `week_of` convention so the `agent_outputs.wins` rows align on the
    # same weekly bucket regardless of writer.
    week_of = today - timedelta(days=today.weekday())
    win_id = f"mcp-{week_of.isoformat()}-{title_hash12}"

    # Dedup via the deterministic win_id. Same title in the same week
    # from MCP collapses to one row; new week → new row.
    existing = query_rows(
        f"SELECT win_id FROM `{cfg.project_id}.{cfg.outputs_dataset}.wins` "  # noqa: S608
        f"WHERE win_id = @wid LIMIT 1",
        parameters=[{"name": "wid", "type": "STRING", "value": win_id}],
    )
    if existing:
        return {
            "win_id": existing[0]["win_id"],
            "inserted": False,
            "duplicate": True,
        }

    # `source_kind='mcp'` documents the origin distinctly from Brag
    # Spotter's enum (triaged_item|routed_event|note|decision|reflection).
    # `summary` is the schema's longer-form-text column; the public arg
    # stays `context` for natural English.
    sql = (
        f"INSERT INTO `{cfg.project_id}.{cfg.outputs_dataset}.wins` "  # noqa: S608
        f"(win_id, captured_at, week_of, source_kind, source_id, title, summary) "
        f"VALUES (@wid, @now, @week_of, 'mcp', @source, @title, @summary)"
    )
    query_rows(
        sql,
        parameters=[
            {"name": "wid", "type": "STRING", "value": win_id},
            {"name": "now", "type": "TIMESTAMP", "value": now.isoformat()},
            {"name": "week_of", "type": "DATE", "value": week_of.isoformat()},
            {"name": "source", "type": "STRING", "value": source_id or "mcp-server"},
            {"name": "title", "type": "STRING", "value": title},
            {"name": "summary", "type": "STRING", "value": context or ""},
        ],
    )

    # ADR 0052 — synthetic notes row for brain_ask retrievability.
    # Best-effort; embedder failures don't fail the parent win write.
    synthetic_note_id = f"syn-win-{win_id}"
    note_inserted, note_error = _insert_synthetic_note(
        note_id=synthetic_note_id,
        note_kind="win",
        source_record_id=win_id,
        title=title,
        body=context or "",
        revision_id=now.isoformat(),
        extraction_method="synthetic-win-v1",
    )
    return {
        "win_id": win_id,
        "inserted": True,
        "duplicate": False,
        "synthetic_note_id": synthetic_note_id if note_inserted else None,
        "synthetic_note_error": note_error,
    }


# ADR 0060 — feedback verdict enums. Kept in sync with the
# `agent_outputs.signal_feedback` schema (terraform agent_runtime module)
# and the Risk Watcher suppression gate.
_FEEDBACK_SCOPES = ("risk", "triage", "draft")
_FEEDBACK_VERDICTS = ("noise", "valid", "wrong_tone", "wrong_target")


def record_feedback(
    scope: str,
    verdict: str,
    account_name: str | None = None,
    pattern_name: str | None = None,
    note: str | None = None,
    mute_days: int | None = None,
    source_flag_id: str | None = None,
) -> dict[str, Any]:
    """Record the operator's verdict on a signal so the agents stop re-surfacing noise (ADR 0060).

    USE THIS WHEN the operator reacts to a risk flag, a triaged item, or a
    drafted reply with a judgment the system should remember. Trigger phrases:
      - "that <account> <pattern> flag is noise" / "stop flagging <account>"
      - "mute the acknowledgment-gap flag on <account> for a month"
      - "that was a good catch" (verdict='valid')
      - "wrong tone on that draft" (verdict='wrong_tone')
      - "that flag was about the wrong person/account" (verdict='wrong_target')

    DO NOT USE FOR:
      - Resolving a single flag instance you just want off today's brief —
        that's a one-off; this records a *standing* verdict on the recurring
        (account, pattern) tuple. A 'noise' verdict here suppresses FUTURE
        emissions until ``mute_days`` elapses (or forever if omitted).
      - Capturing a general thought — use ``capture_note``.
      - Acting on an Airtable Task — use the Airtable MCP.

    Only ``verdict='noise'`` drives suppression (Risk Watcher writes matching
    flags pre-resolved so they never reach the brief or the fan-out — ADR 0060
    §3). The other verdicts are recorded for Stage-2 tuning (ADR 0060 §4) and
    are no-ops on emission today.

    Safety wrapper (matches the other write tools): ``scope`` and ``verdict``
    are validated against fixed enums (prompt-injection guard); ``created_by``
    is forced to ``'operator'``; a duplicate verdict on the same
    ``(scope, account_id, pattern_name, verdict)`` within 60s returns the
    existing ``feedback_id`` instead of inserting again.

    Args:
        scope: ``"risk"`` (a Risk Watcher flag), ``"triage"`` (a triaged item),
            or ``"draft"`` (a drafted reply's tone/target).
        verdict: ``"noise"`` | ``"valid"`` | ``"wrong_tone"`` | ``"wrong_target"``.
        account_name: The account the verdict is about (e.g. ``"Client A"``).
            Resolved to the Airtable record id. Omit only for account-agnostic
            draft-tone feedback.
        pattern_name: For ``scope='risk'``, the flag's ``pattern_name``
            EXACTLY as it appears in ``open_risk_flags`` output (the
            suppression gate matches it verbatim) — real values are
            human-readable title case like ``"Owner Disengagement"``, NOT
            snake_case. Omit to mute ALL patterns on the account (the safe
            fallback when unsure of the exact string).
        note: Optional free-text rationale ("they always go quiet in summer").
        mute_days: For ``verdict='noise'``, suppress for this many days. Omit
            for an indefinite mute (use sparingly — a finite window is safer).
        source_flag_id: Optional ``flag_id`` / triaged-item id that prompted
            the verdict, for traceability.

    Returns:
        ``{"feedback_id": str, "inserted": bool, "duplicate": bool,
        "account_id": str | None, "mute_until": str | None}``. On a resolution
        or validation failure, ``{"inserted": False, "error": "..."}``.
    """
    if scope not in _FEEDBACK_SCOPES:
        return {
            "inserted": False,
            "error": f"scope must be one of {_FEEDBACK_SCOPES}, got {scope!r}",
        }
    if verdict not in _FEEDBACK_VERDICTS:
        return {
            "inserted": False,
            "error": f"verdict must be one of {_FEEDBACK_VERDICTS}, got {verdict!r}",
        }
    if mute_days is not None and (not isinstance(mute_days, int) or mute_days <= 0):
        return {"inserted": False, "error": f"mute_days must be a positive int, got {mute_days!r}"}

    cfg = get_config()

    # Resolve account_name -> Airtable record id via the replica (same JOIN
    # `open_risk_flags` uses: risk_flags.account_id = accounts._airtable_record_id).
    # Case-insensitive to be forgiving of how the operator types the name.
    account_id: str | None = None
    if account_name:
        rows = query_rows(
            f"SELECT DISTINCT _airtable_record_id FROM "  # noqa: S608
            f"`{cfg.project_id}.airtable_replica.accounts` "
            f"WHERE LOWER(company_name) = LOWER(@name) LIMIT 2",
            parameters=[{"name": "name", "type": "STRING", "value": account_name.strip()}],
        )
        if not rows:
            return {
                "inserted": False,
                "error": f"no account matched company_name {account_name!r}",
            }
        if len(rows) > 1:
            return {
                "inserted": False,
                "error": f"account_name {account_name!r} is ambiguous (matched >1 record); "
                "be more specific",
            }
        account_id = rows[0]["_airtable_record_id"]

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    mute_until_iso: str | None = None
    if verdict == "noise" and mute_days is not None:
        from datetime import timedelta

        mute_until_iso = (now + timedelta(days=mute_days)).isoformat()

    # 60s dedup — a repeated verdict on the same tuple (e.g. an LLM retry)
    # collapses rather than stacking rows. NULL-safe equality so account-/
    # pattern-agnostic verdicts dedup too.
    existing = query_rows(
        f"SELECT feedback_id FROM `{cfg.project_id}.{cfg.outputs_dataset}.signal_feedback` "  # noqa: S608
        f"WHERE scope = @scope AND verdict = @verdict "
        f"AND account_id IS NOT DISTINCT FROM @account_id "
        f"AND pattern_name IS NOT DISTINCT FROM @pattern_name "
        f"AND created_at >= TIMESTAMP_SUB(@now, INTERVAL 60 SECOND) "
        f"ORDER BY created_at DESC LIMIT 1",
        parameters=[
            {"name": "scope", "type": "STRING", "value": scope},
            {"name": "verdict", "type": "STRING", "value": verdict},
            {"name": "account_id", "type": "STRING", "value": account_id},
            {"name": "pattern_name", "type": "STRING", "value": pattern_name},
            {"name": "now", "type": "TIMESTAMP", "value": now_iso},
        ],
    )
    if existing:
        return {
            "feedback_id": existing[0]["feedback_id"],
            "inserted": False,
            "duplicate": True,
            "account_id": account_id,
            "mute_until": mute_until_iso,
        }

    feedback_id = f"fb-{uuid.uuid4().hex[:12]}"
    query_rows(
        f"INSERT INTO `{cfg.project_id}.{cfg.outputs_dataset}.signal_feedback` "  # noqa: S608
        f"(feedback_id, created_at, scope, account_id, pattern_name, verdict, "
        f"note, mute_until, source_flag_id, created_by) "
        f"VALUES (@feedback_id, @now, @scope, @account_id, @pattern_name, @verdict, "
        f"@note, @mute_until, @source_flag_id, 'operator')",
        parameters=[
            {"name": "feedback_id", "type": "STRING", "value": feedback_id},
            {"name": "now", "type": "TIMESTAMP", "value": now_iso},
            {"name": "scope", "type": "STRING", "value": scope},
            {"name": "account_id", "type": "STRING", "value": account_id},
            {"name": "pattern_name", "type": "STRING", "value": pattern_name},
            {"name": "verdict", "type": "STRING", "value": verdict},
            {"name": "note", "type": "STRING", "value": note},
            {"name": "mute_until", "type": "TIMESTAMP", "value": mute_until_iso},
            {"name": "source_flag_id", "type": "STRING", "value": source_flag_id},
        ],
    )
    return {
        "feedback_id": feedback_id,
        "inserted": True,
        "duplicate": False,
        "account_id": account_id,
        "mute_until": mute_until_iso,
    }
