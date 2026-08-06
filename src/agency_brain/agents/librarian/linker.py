"""Semantic linker for the Librarian (ADR 0038 §5 + the daily-reflection-doc plan).

After Librarian moves a file into an Areas folder, the linker:

  1. Pulls the file's row from ``agent_outputs.notes`` (it was just
     ingested by the daily Notes Ingestor tick — Phase A).
  2. Runs ``VECTOR_SEARCH`` against the corpus to find top-K semantic
     neighbors (cosine ≥ ``LIBRARIAN_LINK_COSINE_THRESHOLD``, default
     0.78 per ADR 0038).
  3. Writes BIDIRECTIONAL rows into ``agent_outputs.notes_links``
     (source → target AND target → source), keyed for idempotent re-runs.
  4. Optionally hands the neighbor list to a dossier-section editor so
     the destination dossier's ``## Related`` block surfaces backlinks.

Pre-INSERT dedup: a per-pair SELECT keeps re-runs from duplicating
rows. The current ``notes_links`` schema (ADR 0038 §5) is 4 columns —
``source_note_id``, ``target_note_id``, ``similarity``, ``computed_at``;
adding an explicit ``link_type`` would be a follow-up TF migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .models import LinkerOutcome

log = logging.getLogger("agency_brain.agents.librarian.linker")


DEFAULT_TOP_K = 3
DEFAULT_COSINE_THRESHOLD = 0.78
"""ADR 0038 §2 — cosine *similarity* threshold. We compute distance
(``1 - similarity``) in the SQL; threshold is applied as
``distance <= 1 - DEFAULT_COSINE_THRESHOLD``. Tunable via env var
``LIBRARIAN_LINK_COSINE_THRESHOLD`` for early-corpus noise mitigation."""


@dataclass(frozen=True)
class NeighborRow:
    """One row from the VECTOR_SEARCH result."""

    note_id: str
    filename: str
    distance: float
    source_drive_url: str | None = None


class BQQueryClient(Protocol):
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]: ...


class BQRowsClient(Protocol):
    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list: ...


class DossierEditor(Protocol):
    """Loose Protocol so tests can pass a fake without importing Docs API."""

    def update_related_section(self, *, doc_id: str, body_lines: list[str]) -> object: ...


class LibrarianLinker:
    """Populates ``notes_links`` and (optionally) updates dossier `## Related`."""

    def __init__(
        self,
        *,
        bq_query: BQQueryClient,
        bq_writer: BQRowsClient,
        project_id: str,
        notes_table: str = "notes",
        links_table: str = "notes_links",
        dataset_id: str = "agent_outputs",
        top_k: int = DEFAULT_TOP_K,
        cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
        dossier_editor: DossierEditor | None = None,
    ) -> None:
        self._bq_query = bq_query
        self._bq_writer = bq_writer
        self._notes_ref = f"{project_id}.{dataset_id}.{notes_table}"
        self._links_ref = f"{project_id}.{dataset_id}.{links_table}"
        self._top_k = top_k
        self._cosine_threshold = cosine_threshold
        self._dossier_editor = dossier_editor

    # -------------------------------------------------------------- public

    def link_for_drive_file(
        self,
        *,
        drive_file_id: str,
        dossier_doc_id: str | None = None,
    ) -> LinkerOutcome:
        """Run the link pipeline for a single file freshly moved into Areas/.

        Returns a ``LinkerOutcome`` with bookkeeping fields the caller's
        audit row consumes. Failures degrade gracefully — the move
        already happened, the link layer is best-effort.
        """
        note = self._lookup_note_for_drive_file(drive_file_id)
        if note is None:
            log.info(
                "librarian.linker.skip note not yet in agent_outputs.notes "
                "(daily ingest probably hasn't fired since the move) drive_file=%s",
                drive_file_id,
            )
            return LinkerOutcome()
        neighbors = self._find_neighbors(
            embedding=note["embedding"],
            self_note_id=note["note_id"],
        )
        if not neighbors:
            log.info("librarian.linker.no_neighbors note_id=%s", note["note_id"])
            return self._maybe_update_dossier(
                dossier_doc_id=dossier_doc_id,
                source_filename=note["filename"],
                neighbors=(),
            )

        existing_pairs = self._existing_pair_keys(note["note_id"], neighbors)
        rows: list[dict] = []
        now = datetime.now(UTC).isoformat()
        for n in neighbors:
            forward = (note["note_id"], n.note_id)
            backward = (n.note_id, note["note_id"])
            if forward not in existing_pairs:
                rows.append(_link_row(forward[0], forward[1], n.distance, now))
            if backward not in existing_pairs:
                rows.append(_link_row(backward[0], backward[1], n.distance, now))
        inserted = 0
        if rows:
            errors = self._bq_writer.insert_rows_json(self._links_ref, rows)
            if errors:
                log.warning("librarian.linker.insert_partial errors=%s", errors)
            inserted = len(rows) - len(errors or [])

        return self._maybe_update_dossier(
            dossier_doc_id=dossier_doc_id,
            source_filename=note["filename"],
            neighbors=tuple(neighbors),
            neighbors_linked=inserted,
        )

    # -------------------------------------------------------------- helpers

    def _lookup_note_for_drive_file(self, drive_file_id: str) -> dict | None:
        # Require the full 768-dim vector (text-embedding-005, ADR 0038):
        # this row becomes the @query_embedding for VECTOR_SEARCH, and a
        # short/truncated vector makes the function error at query time
        # (the whole link step then fails and silently links nothing).
        # Filtering here turns a partial embedding into "no neighbors"
        # rather than a swallowed exception.
        sql = (
            f"SELECT note_id, filename, embedding "
            f"FROM `{self._notes_ref}` "
            "WHERE source_drive_file_id = @drive_file_id "
            "AND ARRAY_LENGTH(embedding) = 768 "
            "ORDER BY ingested_at DESC LIMIT 1"
        )
        params = [{"name": "drive_file_id", "type": "STRING", "value": drive_file_id}]
        try:
            rows = self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("librarian.linker.notes_lookup_failed drive_file=%s", drive_file_id)
            return None
        if not rows:
            return None
        row = rows[0]
        emb = row.get("embedding") or []
        if len(emb) != 768:
            return None
        return {
            "note_id": str(row.get("note_id") or ""),
            "filename": str(row.get("filename") or ""),
            "embedding": list(emb),
        }

    def _find_neighbors(self, *, embedding: list[float], self_note_id: str) -> list[NeighborRow]:
        max_distance = max(0.0, min(2.0, 1.0 - self._cosine_threshold))
        # VECTOR_SEARCH validates dimension across the entire base table
        # BEFORE the inline WHERE filter is applied. Pre-filter via a
        # subquery so the function only sees rows with the expected
        # 768-dim embedding (text-embedding-005 per ADR 0038). Also
        # excludes hipaa-isolated rows + the source note itself here so
        # they're never weighed against by the cosine compute. Note:
        # ``scope`` filter dropped (was 'personal' only — too narrow
        # post-Phase G when client/agency notes also become corpus).
        sql = f"""
WITH search AS (
  SELECT base.note_id,
         base.filename,
         base.source_drive_url,
         distance
  FROM VECTOR_SEARCH(
    (
      SELECT *
      FROM `{self._notes_ref}`
      WHERE ARRAY_LENGTH(embedding) = 768
        AND hipaa_isolated = FALSE
        AND note_id != @self_id
    ),
    'embedding',
    (SELECT @query_embedding AS embedding),
    top_k => @top_k_plus_one,
    distance_type => 'COSINE'
  )
)
SELECT note_id, filename, source_drive_url, distance
FROM search
WHERE distance <= @max_distance
ORDER BY distance ASC
LIMIT @top_k
"""  # noqa: S608
        params = [
            {"name": "query_embedding", "type": "ARRAY_FLOAT64", "value": embedding},
            {"name": "top_k", "type": "INT64", "value": self._top_k},
            {"name": "top_k_plus_one", "type": "INT64", "value": self._top_k + 1},
            {"name": "self_id", "type": "STRING", "value": self_note_id},
            {"name": "max_distance", "type": "FLOAT64", "value": max_distance},
        ]
        try:
            rows = self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("librarian.linker.vector_search_failed self=%s", self_note_id)
            return []
        return [
            NeighborRow(
                note_id=str(r.get("note_id") or ""),
                filename=str(r.get("filename") or ""),
                distance=float(r.get("distance") or 0.0),
                source_drive_url=r.get("source_drive_url") or None,
            )
            for r in rows
            if r.get("note_id")
        ]

    def _existing_pair_keys(
        self, source_id: str, neighbors: list[NeighborRow]
    ) -> set[tuple[str, str]]:
        if not neighbors:
            return set()
        ids = [source_id] + [n.note_id for n in neighbors]
        sql = (
            f"SELECT source_note_id, target_note_id "
            f"FROM `{self._links_ref}` "
            "WHERE source_note_id IN UNNEST(@ids) "
            "OR target_note_id IN UNNEST(@ids)"
        )
        params = [{"name": "ids", "type": "ARRAY_STRING", "value": ids}]
        try:
            rows = self._bq_query.query_rows(sql, parameters=params)
        except Exception:
            log.exception("librarian.linker.existing_pairs_lookup_failed")
            return set()
        return {
            (str(r.get("source_note_id") or ""), str(r.get("target_note_id") or "")) for r in rows
        }

    def _maybe_update_dossier(
        self,
        *,
        dossier_doc_id: str | None,
        source_filename: str,
        neighbors: tuple[NeighborRow, ...],
        neighbors_linked: int = 0,
    ) -> LinkerOutcome:
        if not dossier_doc_id or self._dossier_editor is None:
            return LinkerOutcome(
                neighbors_linked=neighbors_linked,
                dossier_doc_id=dossier_doc_id,
            )
        body_lines: list[str] = []
        # Lead with the file we just sorted in (so "Related" reflects the
        # most recent classification visibly), then its top neighbors.
        if source_filename:
            body_lines.append(source_filename)
        for n in neighbors:
            label = n.filename or n.note_id
            if n.source_drive_url:
                # Plain text body — the editor renders ``- {line}``; URLs
                # show up as Drive-clickable hyperlinks once Docs auto-detects.
                body_lines.append(f"{label} — {n.source_drive_url}")
            else:
                body_lines.append(label)
        try:
            self._dossier_editor.update_related_section(
                doc_id=dossier_doc_id,
                body_lines=body_lines,
            )
        except Exception:
            log.exception("librarian.linker.dossier_edit_failed doc_id=%s", dossier_doc_id)
            return LinkerOutcome(
                neighbors_linked=neighbors_linked,
                dossier_doc_id=dossier_doc_id,
                related_section_updated=False,
            )
        return LinkerOutcome(
            neighbors_linked=neighbors_linked,
            dossier_doc_id=dossier_doc_id,
            related_section_updated=True,
        )


def _link_row(source_id: str, target_id: str, distance: float, now_iso: str) -> dict:
    """Mirror the ADR 0038 §5 ``agent_outputs.notes_links`` schema (4 cols)."""
    return {
        "source_note_id": source_id,
        "target_note_id": target_id,
        "similarity": max(0.0, 1.0 - distance),
        "computed_at": now_iso,
    }
