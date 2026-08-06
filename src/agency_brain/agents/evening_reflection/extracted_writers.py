"""BQ writers for REFLECT-mode extracted decisions/wins (ADR 0040 §6).

Mirrors the ``captures_materializer/dispatch.py`` writer pattern:
parameterized pre-INSERT SELECT (deterministic idempotency key) →
``insert_rows_json`` if absent, no-op if present. Lifts the
column-whitelisted ``_row_exists`` shape so PR-B inherits ADR 0039
§3's safety posture.

Idempotency-key format (ADR 0040 §6):
``decision_id = f"reflection-{voice_note_id or reflection_id}-{title_hash12}"``
``win_id     = f"reflection-{voice_note_id or reflection_id}-{title_hash12}"``

``title_hash12`` = ``sha256(normalize(title)).hexdigest()[:12]`` where
normalize = lowercase / strip / collapse whitespace / strip punctuation /
truncate 64 chars before hashing. Trivial LLM output variation (trailing
period, double space, capitalization) collapses to the same key so
re-ticks don't double-write.
"""

from __future__ import annotations

import hashlib
import logging
import re
import string
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .models import ExtractedDecision, ExtractedWin

log = logging.getLogger("agency_brain.agents.evening_reflection.extracted_writers")


# ---------------------------------------------------------------------------
# BQ surfaces (Protocol shapes match the captures_materializer dispatcher)
# ---------------------------------------------------------------------------


class BQRowsClient(Protocol):
    """Streaming-insert surface; matches ``insert_rows_json``."""

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class BQQueryClient(Protocol):
    """Parameterized SELECT for the pre-INSERT dedup check."""

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Idempotency key construction
# ---------------------------------------------------------------------------


_PUNCT_TRANS = str.maketrans("", "", string.punctuation)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """ADR 0040 §6 normalize step.

    Lowercase + collapse whitespace + strip punctuation + truncate 64
    chars (before hashing). Stable under trivial LLM output variation
    (trailing period, double space, capitalization, smart quotes — the
    last via ASCII-only punctuation strip).
    """
    s = title.strip().lower()
    s = s.translate(_PUNCT_TRANS)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if len(s) > 64:
        s = s[:64]
    return s


def title_hash12(title: str) -> str:
    """SHA-256 of the normalized title, first 12 hex chars."""
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()[:12]


def decision_id_for(*, reflection_id: str, voice_note_id: str | None, title: str) -> str:
    """Build the deterministic ``decision_id`` for a reflection-extracted decision.

    Falls back to ``reflection_id`` when the LLM didn't attribute the
    decision to a specific voice memo (per ADR 0040 §6).
    """
    anchor = voice_note_id or reflection_id
    return f"reflection-{anchor}-{title_hash12(title)}"


def win_id_for(*, reflection_id: str, voice_note_id: str | None, title: str) -> str:
    """Build the deterministic ``win_id`` for a reflection-extracted win."""
    anchor = voice_note_id or reflection_id
    return f"reflection-{anchor}-{title_hash12(title)}"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _monday_of_iso_week(d: date) -> date:
    """ISO Monday of the week containing ``d``. ``date.weekday()`` returns
    0 for Monday, so subtracting that many days lands on Monday."""
    return d - timedelta(days=d.weekday())


def build_decision_row(
    extracted: ExtractedDecision,
    *,
    reflection_id: str,
    decision_id: str,
    agent_run_id: str | None,
    now: datetime,
) -> dict:
    """Map an ``ExtractedDecision`` onto an ``agent_outputs.decisions`` row.

    Sets ``status='draft'`` so the row enters the same review pipeline
    as Captures-form decisions (ADR 0039); the user fills in alternatives
    + prediction + confidence later via Phase 2 reviewer (ADR 0041) or
    BQ console. Schedules 30/90/365-day retro dates from ``now`` so
    the Phase 2 reviewer surfaces them on cadence.

    ``source_reflection_id`` and ``source_voice_note_id`` are populated
    so the provenance trail is intact: 30d retros can JOIN back to the
    reflection that birthed the decision and (when applicable) the
    voice memo it originated from.
    """
    captured_date = now.date()
    return {
        "decision_id": decision_id,
        "decided_at": now.isoformat(),
        "title": extracted.title,
        "context": extracted.context,
        "alternatives": [],
        "choice": extracted.context or extracted.title,
        "prediction": None,
        "confidence": None,
        "review_30_at": (captured_date + timedelta(days=30)).isoformat(),
        "review_90_at": (captured_date + timedelta(days=90)).isoformat(),
        "review_365_at": (captured_date + timedelta(days=365)).isoformat(),
        "status": "draft",
        "source_reflection_id": reflection_id,
        "source_voice_note_id": extracted.source_voice_note_id,
        "refined_at": None,
        "retrospective_30": None,
        "retrospective_90": None,
        "retrospective_365": None,
        "calibration_score": None,
        "agent_run_id": agent_run_id,
    }


def build_win_row(
    extracted: ExtractedWin,
    *,
    reflection_id: str,
    win_id: str,
    agent_run_id: str | None,
    now: datetime,
) -> dict:
    """Map an ``ExtractedWin`` onto an ``agent_outputs.wins`` row.

    ``source_kind='reflection'`` so the Brag Spotter (Phase 3) can
    aggregate by extraction source. ``source_id`` carries the
    reflection_id for the JOIN-back. ``week_of`` is the ISO Monday of
    today.
    """
    return {
        "win_id": win_id,
        "captured_at": now.isoformat(),
        "week_of": _monday_of_iso_week(now.date()).isoformat(),
        "source_kind": "reflection",
        "source_id": reflection_id,
        "title": extracted.title,
        "summary": extracted.summary,
        "evidence_links": [],
        "agent_run_id": agent_run_id,
    }


# ---------------------------------------------------------------------------
# Pre-INSERT dedup SELECT (ADR 0040 §6, lifted from captures_materializer)
# ---------------------------------------------------------------------------


def _row_exists(
    bq_query: BQQueryClient,
    *,
    table_ref: str,
    column: str,
    value: str,
) -> bool:
    """Pre-INSERT dedup SELECT.

    The column whitelist is ``decision_id``/``win_id``; the table_ref
    is constructed by ``main.py`` from BRAIN_PROJECT_ID + the fixed
    dataset/table names. The column is interpolated literally (limited
    to known values), and the value is parameterized — no
    user-controlled SQL surface.
    """
    if column not in {"decision_id", "win_id"}:
        raise ValueError(f"refusing to dedup on unknown column: {column!r}")
    sql = (
        f"SELECT 1 FROM `{table_ref}` "  # noqa: S608 — column whitelisted
        f"WHERE {column} = @value LIMIT 1"
    )
    rows = bq_query.query_rows(
        sql,
        parameters=[{"name": "value", "type": "STRING", "value": value}],
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Writer classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteOutcome:
    """One per attempted INSERT — bookkeeping for the dispatcher."""

    target_id: str
    written: bool  # False on dedup hit
    error: str | None = None


class DecisionsWriter:
    """Inserts an ``agent_outputs.decisions`` row (or no-ops on dedup hit)."""

    def __init__(
        self,
        *,
        bq: BQRowsClient,
        bq_query: BQQueryClient,
        table_ref: str,
    ) -> None:
        self._bq = bq
        self._bq_query = bq_query
        self._table_ref = table_ref

    def write(
        self,
        extracted: ExtractedDecision,
        *,
        reflection_id: str,
        agent_run_id: str | None,
        now: datetime | None = None,
    ) -> WriteOutcome:
        decision_id = decision_id_for(
            reflection_id=reflection_id,
            voice_note_id=extracted.source_voice_note_id,
            title=extracted.title,
        )
        try:
            if _row_exists(
                self._bq_query,
                table_ref=self._table_ref,
                column="decision_id",
                value=decision_id,
            ):
                return WriteOutcome(target_id=decision_id, written=False)
            row = build_decision_row(
                extracted,
                reflection_id=reflection_id,
                decision_id=decision_id,
                agent_run_id=agent_run_id,
                now=now or datetime.now(UTC),
            )
            errors = self._bq.insert_rows_json(self._table_ref, [row])
            if errors:
                raise RuntimeError(f"BQ rejected decisions insert: {errors}")
            return WriteOutcome(target_id=decision_id, written=True)
        except Exception as exc:  # — dispatcher aggregates
            log.exception(
                "evening_reflection.decisions_writer.write_failed decision_id=%s",
                decision_id,
            )
            return WriteOutcome(
                target_id=decision_id,
                written=False,
                error=f"{type(exc).__name__}: {exc}",
            )


class WinsWriter:
    """Inserts an ``agent_outputs.wins`` row (or no-ops on dedup hit)."""

    def __init__(
        self,
        *,
        bq: BQRowsClient,
        bq_query: BQQueryClient,
        table_ref: str,
    ) -> None:
        self._bq = bq
        self._bq_query = bq_query
        self._table_ref = table_ref

    def write(
        self,
        extracted: ExtractedWin,
        *,
        reflection_id: str,
        agent_run_id: str | None,
        now: datetime | None = None,
    ) -> WriteOutcome:
        win_id = win_id_for(
            reflection_id=reflection_id,
            voice_note_id=extracted.source_voice_note_id,
            title=extracted.title,
        )
        try:
            if _row_exists(
                self._bq_query,
                table_ref=self._table_ref,
                column="win_id",
                value=win_id,
            ):
                return WriteOutcome(target_id=win_id, written=False)
            row = build_win_row(
                extracted,
                reflection_id=reflection_id,
                win_id=win_id,
                agent_run_id=agent_run_id,
                now=now or datetime.now(UTC),
            )
            errors = self._bq.insert_rows_json(self._table_ref, [row])
            if errors:
                raise RuntimeError(f"BQ rejected wins insert: {errors}")
            return WriteOutcome(target_id=win_id, written=True)
        except Exception as exc:  # — dispatcher aggregates
            log.exception(
                "evening_reflection.wins_writer.write_failed win_id=%s",
                win_id,
            )
            return WriteOutcome(
                target_id=win_id,
                written=False,
                error=f"{type(exc).__name__}: {exc}",
            )


# Re-exports a Vertex-irrelevant helper for tests + dispatch convenience.
__all__ = [
    "BQQueryClient",
    "BQRowsClient",
    "DecisionsWriter",
    "WinsWriter",
    "WriteOutcome",
    "build_decision_row",
    "build_win_row",
    "decision_id_for",
    "normalize_title",
    "title_hash12",
    "win_id_for",
]
