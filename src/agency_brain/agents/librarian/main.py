"""Cloud Run Job entrypoint for the Librarian (ADR 0044, daily 5am PT).

One Job execution = one Librarian tick.

Per tick:
  1. List drop files (and stale QuickNotes if env-gated).
  2. Index Areas/* destinations (cached per-tick).
  3. For each file:
     a. Download + extract via notes_ingestor.extractor (markdown).
     b. Classify via Vertex Gemini 2.5 Flash + response_schema.
     c. Confidence ≥ threshold → move to dest; else → _uncategorized/.
     d. Linker: VECTOR_SEARCH neighbors + bidirectional notes_links INSERT
        + auto-edit dossier "## Related" section.
     e. Audit row.
  4. Summary audit row.

Required env vars (per ADR 0044 + plan):
  - BRAIN_PROJECT_ID
  - BRAIN_AREAS_FOLDER_ID                (the Areas root)
  - BRAIN_INBOX_DROP_FOLDER_ID           (Drop is Librarian-owned)
  - BRAIN_INBOX_QUICKNOTES_FOLDER_ID     (optional, age-based sweep)
  - LIBRARIAN_QUICKNOTES_AGE_DAYS        (default 14)
  - LIBRARIAN_MAX_PER_TICK               (default 50)
  - LIBRARIAN_CONFIDENCE_THRESHOLD       (default 0.6)
  - LIBRARIAN_LINK_TOP_K                 (default 3)
  - LIBRARIAN_LINK_COSINE_THRESHOLD      (default 0.78 per ADR 0038)
  - LIBRARIAN_DOSSIER_FILENAME           (default "dossier.gdoc"; the
    file name the linker looks for inside each Areas/<topic>/ folder
    when wiring the auto-edit "## Related" section. Empty disables the
    dossier edit step entirely.)
  - BRAIN_GALAXY_FOLDER_ID               (optional, ADR 0054 §2; when
    set, the Librarian also sweeps Brain/05_GALAXY/ recursively and
    indexes each file with note_kind='galaxy'. No move, no classifier.
    Empty disables the sweep silently.)
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from typing import Any

log = logging.getLogger("agency_brain.agents.librarian.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    # Phase G — multi-root: prefer LIBRARIAN_DEST_ROOTS=label:id,label:id;
    # fall back to legacy single-root BRAIN_AREAS_FOLDER_ID for back-compat.
    dest_roots_raw = os.environ.get("LIBRARIAN_DEST_ROOTS", "").strip()
    legacy_areas_root_id = os.environ.get("BRAIN_AREAS_FOLDER_ID", "").strip()
    excluded_names_raw = os.environ.get("LIBRARIAN_EXCLUDED_FOLDER_NAMES", "").strip()

    drop_folder_id = os.environ.get("BRAIN_INBOX_DROP_FOLDER_ID", "").strip()
    quicknotes_folder_id = os.environ.get("BRAIN_INBOX_QUICKNOTES_FOLDER_ID", "").strip()
    galaxy_folder_id = os.environ.get("BRAIN_GALAXY_FOLDER_ID", "").strip()
    quicknotes_age_days = _int_env("LIBRARIAN_QUICKNOTES_AGE_DAYS", 14)
    max_per_tick = _int_env("LIBRARIAN_MAX_PER_TICK", 50)
    confidence_threshold = _float_env("LIBRARIAN_CONFIDENCE_THRESHOLD", 0.6)
    link_top_k = _int_env("LIBRARIAN_LINK_TOP_K", 3)
    link_cosine = _float_env("LIBRARIAN_LINK_COSINE_THRESHOLD", 0.78)
    dossier_filename = os.environ.get("LIBRARIAN_DOSSIER_FILENAME", "dossier.gdoc").strip()
    sa_email = os.environ.get("LIBRARIAN_SA_EMAIL") or (
        f"asb-librarian-sa@{project_id}.iam.gserviceaccount.com"
    )

    if not dest_roots_raw and not legacy_areas_root_id:
        log.warning(
            "librarian.skip: neither LIBRARIAN_DEST_ROOTS nor BRAIN_AREAS_FOLDER_ID set; nothing to do"
        )
        return 0

    watched: list[tuple[str, str]] = []
    if drop_folder_id:
        watched.append((drop_folder_id, "drop"))
    if quicknotes_folder_id:
        watched.append((quicknotes_folder_id, "quicknotes"))
    if not watched:
        log.warning("librarian.skip: no watched folders configured")
        return 0

    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    log.info(
        "librarian.start run_id=%s project=%s drop=%s quicknotes=%s dest_roots=%r",
        run_id,
        project_id,
        drop_folder_id,
        quicknotes_folder_id,
        dest_roots_raw or legacy_areas_root_id,
    )

    # Lazy imports — keep cold-start light.
    from google.cloud import bigquery

    from ...common.audit_log import AuditLogClient
    from ...common.dossier_section_editor import DossierSectionEditor
    from ..notes_ingestor.drive_client import ADCDriveServiceFactory
    from ..notes_ingestor.embedder import VertexEmbedder
    from ..notes_ingestor.extractor import (
        GeminiMultimodalExtractor,
        VertexMultimodalLLM,
    )
    from ..notes_ingestor.writer import NotesWriter
    from .areas_index import (
        LibrarianAreasIndex,
        parse_excluded_names_env,
        parse_roots_env,
    )
    from .classifier import (
        LibrarianClassifier,
        LibrarianClassifyConfig,
        VertexLibrarianClassifier,
    )
    from .drive_lister import LibrarianDriveLister
    from .ingestor import DriveMetaAdapter, LibrarianIngestor
    from .linker import LibrarianLinker
    from .models import (
        LibrarianSummary,
    )
    from .mover import LibrarianMover
    from .writer import LibrarianAuditWriter

    drive_factory = ADCDriveServiceFactory()
    drive_service = drive_factory.build()
    drive_client = _DriveAdapter(drive_service)
    bq_client = bigquery.Client(project=project_id)

    # Phase G+ — query the audit log for file_ids the Librarian has
    # already successfully classified+moved, so re-listing the same
    # file (when its original is still in 06_DROP because the
    # cross-Shared-Drive copy fallback can't delete it) doesn't create
    # an additional copy at the destination on every tick.
    skip_file_ids: set[str] = _load_processed_file_ids(bq_client, project_id=project_id)
    log.info("librarian.skip_set count=%d", len(skip_file_ids))

    lister = LibrarianDriveLister(
        drive_client=drive_client,
        max_per_tick=max_per_tick,
        quicknotes_age_days=quicknotes_age_days,
        skip_file_ids=skip_file_ids,
    )

    # Phase G — destination roots can come from the multi-root env var
    # (preferred) or the legacy single-root BRAIN_AREAS_FOLDER_ID.
    parsed_roots = parse_roots_env(dest_roots_raw)
    if not parsed_roots and legacy_areas_root_id:
        parsed_roots = [("areas", "areas", legacy_areas_root_id)]
    excluded_names = parse_excluded_names_env(excluded_names_raw)

    areas_index = LibrarianAreasIndex(
        drive_client=drive_client,
        roots=parsed_roots,
        excluded_names=excluded_names,
    )
    # The mover still needs a single ``_uncategorized/`` parent. Use the
    # FIRST root in parsed_roots as the home for it. Future tweak: route
    # _uncategorized/ to the same root the file was classified to (when
    # we know — currently None means "no good match anywhere").
    # parsed_roots entries are (bucket, label, folder_id) since ADR 0054.
    primary_root_id = parsed_roots[0][2]

    multimodal_llm = VertexMultimodalLLM(project_id=project_id)
    extractor = GeminiMultimodalExtractor(llm=multimodal_llm)
    vertex_llm = VertexLibrarianClassifier(config=LibrarianClassifyConfig(project_id=project_id))
    classifier = LibrarianClassifier(
        llm=vertex_llm,
        config=LibrarianClassifyConfig(project_id=project_id),
    )
    mover = LibrarianMover(
        service_factory=drive_factory,
        areas_root_id=primary_root_id,
    )

    bq_param = _ParameterizedBQAdapter(bq_client)
    bq_writer = _InsertBQAdapter(bq_client)

    docs_factory = _DocsServiceFactory(scope="https://www.googleapis.com/auth/documents")
    dossier_editor = (
        DossierSectionEditor(service_factory=docs_factory) if dossier_filename else None
    )
    linker = LibrarianLinker(
        bq_query=bq_param,
        bq_writer=bq_writer,
        project_id=project_id,
        top_k=link_top_k,
        cosine_threshold=link_cosine,
        dossier_editor=dossier_editor,
    )

    audit = AuditLogClient(project_id=project_id, bq_client=bq_client)
    audit_writer = LibrarianAuditWriter(audit=audit, sa_email=sa_email)

    # Phase G — Librarian writes the moved file to agent_outputs.notes
    # so it becomes searchable corpus (notes_ingestor doesn't watch the
    # destination folders we're routing into).
    embedder = VertexEmbedder(project_id=project_id)
    notes_writer = NotesWriter(
        bq_rows=bq_writer,
        bq_query=bq_param,
        project_id=project_id,
    )
    drive_meta = DriveMetaAdapter(drive_service)
    ingestor = LibrarianIngestor(
        notes_writer=notes_writer,
        embedder=embedder,
        drive_meta=drive_meta,
    )

    # ----------------------------------------------------------- per-tick
    candidates = areas_index.list_candidates()
    log.info("librarian.candidates count=%d", len(candidates))

    files = lister.list(watched)
    log.info("librarian.listed count=%d", len(files))

    moved = 0
    uncategorized_count = 0
    failed = 0
    neighbors_total = 0
    related_total = 0

    for f in files:
        file_started = time.perf_counter()
        outcome = _process_file(
            drive_file=f,
            drive_client=drive_client,
            extractor=extractor,
            classifier=classifier,
            candidates=candidates,
            mover=mover,
            linker=linker,
            ingestor=ingestor,
            confidence_threshold=confidence_threshold,
            dossier_filename=dossier_filename,
            areas_index=areas_index,
        )
        latency_ms = int((time.perf_counter() - file_started) * 1000)
        audit_writer.emit_file_outcome(run_id=run_id, outcome=outcome, latency_ms=latency_ms)

        if outcome.error is not None:
            failed += 1
            continue
        if outcome.moved:
            moved += 1
            if outcome.to_folder_path == "_uncategorized":
                uncategorized_count += 1
        neighbors_total += outcome.linker.neighbors_linked
        if outcome.linker.related_section_updated:
            related_total += 1

    # ADR 0054 §2 — Galaxy indexer pass. Optional; skipped silently when
    # BRAIN_GALAXY_FOLDER_ID is unset. Reuses the existing ingestor +
    # linker; no classifier, no move.
    galaxy_summary = None
    if galaxy_folder_id:
        from .galaxy_indexer import GalaxyIndexer

        galaxy_indexer = GalaxyIndexer(
            drive_client=drive_client,
            extractor=extractor,
            ingestor=ingestor,
            linker=linker,
            audit_writer=audit_writer,
        )
        galaxy_summary = galaxy_indexer.sweep(run_id=run_id, galaxy_folder_id=galaxy_folder_id)
        log.info(
            "librarian.galaxy.done listed=%d indexed=%d deduped=%d failed=%d neighbors=%d",
            galaxy_summary.listed,
            galaxy_summary.indexed,
            galaxy_summary.deduped,
            galaxy_summary.failed,
            galaxy_summary.neighbors_linked_total,
        )

    summary = LibrarianSummary(
        listed=len(files),
        moved=moved,
        uncategorized=uncategorized_count,
        failed=failed,
        neighbors_linked_total=neighbors_total
        + (galaxy_summary.neighbors_linked_total if galaxy_summary else 0),
        related_sections_updated=related_total,
        galaxy_listed=galaxy_summary.listed if galaxy_summary else 0,
        galaxy_indexed=galaxy_summary.indexed if galaxy_summary else 0,
        galaxy_deduped=galaxy_summary.deduped if galaxy_summary else 0,
        galaxy_failed=galaxy_summary.failed if galaxy_summary else 0,
    )
    total_latency_ms = int((time.perf_counter() - started) * 1000)
    audit_writer.emit_run_summary(run_id=run_id, summary=summary, latency_ms=total_latency_ms)
    log.info(
        "librarian.done run_id=%s listed=%d moved=%d uncategorized=%d "
        "failed=%d neighbors_linked=%d related_updated=%d "
        "galaxy_listed=%d galaxy_indexed=%d galaxy_failed=%d latency_ms=%d",
        run_id,
        summary.listed,
        summary.moved,
        summary.uncategorized,
        summary.failed,
        summary.neighbors_linked_total,
        summary.related_sections_updated,
        summary.galaxy_listed,
        summary.galaxy_indexed,
        summary.galaxy_failed,
        total_latency_ms,
    )
    return 0 if (failed == 0 and summary.galaxy_failed == 0) else 1


# ---------------------------------------------------------------------------
# per-file pipeline (broken out for readability + per-file try/except)
# ---------------------------------------------------------------------------


def _process_file(
    *,
    drive_file,
    drive_client,
    extractor,
    classifier,
    candidates,
    mover,
    linker,
    ingestor,
    confidence_threshold: float,
    dossier_filename: str,
    areas_index,
) -> Any:
    from .areas_index import find_folder_by_path
    from .models import LibrarianOutcome, LinkerOutcome

    try:
        # Extract content.
        markdown = _extract_markdown(
            drive_client=drive_client, extractor=extractor, drive_file=drive_file
        )
        # Classify.
        classification = classifier.classify(
            filename=drive_file.name,
            extracted_markdown=markdown,
            candidates=candidates,
        )
        # Pick destination.
        if classification.dest_folder_path and classification.confidence >= confidence_threshold:
            dest_folder = find_folder_by_path(candidates, classification.dest_folder_path)
        else:
            dest_folder = None

        if dest_folder is None:
            uncat_id = mover.get_or_create_uncategorized()
            mover.move(
                file_id=drive_file.file_id,
                from_folder_id=drive_file.parent_folder_id,
                to_folder_id=uncat_id,
                to_folder_path="_uncategorized",
            )
            ingest_outcome = _safe_ingest(
                ingestor=ingestor,
                drop_file=drive_file,
                dest_folder=None,
                markdown=markdown,
            )
            return LibrarianOutcome(
                file_id=drive_file.file_id,
                file_name=drive_file.name,
                from_folder_role=drive_file.parent_folder_role,
                to_folder_path="_uncategorized",
                confidence=classification.confidence,
                moved=True,
                ingest=ingest_outcome,
            )

        move_result = mover.move(
            file_id=drive_file.file_id,
            from_folder_id=drive_file.parent_folder_id,
            to_folder_id=dest_folder.id,
            to_folder_path=dest_folder.path,
        )

        # Phase G+ — anonymous-filename rename. If the original name
        # looks auto-generated, replace with the canonical convention:
        # YYYY-MM-DD_<topic>_<short-description>.<ext>. Topic derives
        # from the destination path; description from the classifier's
        # suggested_description (or a generic fallback). Applied to
        # the destination file_id (which may be the copy if fallback hit).
        from .renamer import maybe_rename

        new_name = maybe_rename(
            original_name=drive_file.name,
            dest_folder_path=dest_folder.path,
            file_modified_time=drive_file.modified_time,
            suggested_description=classification.suggested_description,
        )
        if new_name and new_name != drive_file.name:
            renamed = mover.rename_file(
                file_id=move_result.file_id,
                new_name=new_name,
            )
            if renamed:
                log.info(
                    "librarian.renamed file=%s '%s' -> '%s'",
                    move_result.file_id,
                    drive_file.name,
                    new_name,
                )

        # Phase G — write to agent_outputs.notes BEFORE the linker runs,
        # so the linker's _lookup_note_for_drive_file finds the freshly
        # ingested row and can compute neighbors against it.
        ingest_outcome = _safe_ingest(
            ingestor=ingestor,
            drop_file=drive_file,
            dest_folder=dest_folder,
            markdown=markdown,
        )

        # Phase G+ — when the move fell back to copy (different file_id
        # returned), the original is still in source. Archive it into
        # source/processed/ so it doesn't re-list on the next tick.
        # Same-Drive move; no cross-drive 403 path. Best-effort.
        #
        # Ordering is load-bearing: archive only AFTER the corpus write
        # is confirmed (written or deduped). Archiving first meant an
        # ingest failure left the original in processed/ — invisible to
        # the next tick — with no corpus row anywhere: silent content
        # loss. On ingest failure the original stays in Drop and the
        # next tick retries the whole file.
        if move_result.file_id != drive_file.file_id and (
            ingest_outcome.written or ingest_outcome.deduped
        ):
            archived = mover.archive_processed_original(
                file_id=drive_file.file_id,
                source_folder_id=drive_file.parent_folder_id,
            )
            if archived:
                log.info(
                    "librarian.archived_original file=%s -> %s/processed",
                    drive_file.file_id,
                    drive_file.parent_folder_id,
                )

        # Linker (best-effort).
        dossier_doc_id = _find_dossier_doc_id(
            drive_client=drive_client,
            folder_id=dest_folder.id,
            dossier_filename=dossier_filename,
        )
        link_outcome = linker.link_for_drive_file(
            drive_file_id=drive_file.file_id,
            dossier_doc_id=dossier_doc_id,
        )
        return LibrarianOutcome(
            file_id=drive_file.file_id,
            file_name=drive_file.name,
            from_folder_role=drive_file.parent_folder_role,
            to_folder_path=dest_folder.path,
            confidence=classification.confidence,
            moved=True,
            linker=link_outcome,
            ingest=ingest_outcome,
        )
    except Exception as exc:
        log.exception("librarian.process_file_failed file_id=%s", drive_file.file_id)
        return LibrarianOutcome(
            file_id=drive_file.file_id,
            file_name=drive_file.name,
            from_folder_role=drive_file.parent_folder_role,
            to_folder_path=None,
            confidence=0.0,
            moved=False,
            linker=LinkerOutcome(),
            error=f"{type(exc).__name__}: {exc}",
        )


def _safe_ingest(*, ingestor, drop_file, dest_folder, markdown):
    """Wrap ingestor.ingest in try/except so write failures don't undo the move."""
    from .models import IngestOutcomeSummary

    try:
        result = ingestor.ingest(
            drop_file=drop_file,
            dest_folder=dest_folder,
            markdown=markdown,
        )
    except Exception as exc:
        log.exception("librarian.ingest_failed file_id=%s", drop_file.file_id)
        return IngestOutcomeSummary(error=f"{type(exc).__name__}: {exc}")
    return IngestOutcomeSummary(
        note_id=result.note_id,
        written=result.written,
        deduped=result.deduped,
        embedded=result.embedded,
        error=result.error,
    )


def _extract_markdown(*, drive_client, extractor, drive_file) -> str:
    """Best-effort content extraction. Returns empty string on failure
    so the classifier falls back to filename-only signal."""
    try:
        # ``GeminiMultimodalExtractor.extract`` accepts a Drive client +
        # file metadata; lift the exact entry point notes_ingestor uses.
        return _extract_via_notes_ingestor(
            drive_client=drive_client, extractor=extractor, drive_file=drive_file
        )
    except Exception:
        log.exception(
            "librarian.extract_failed file_id=%s mime=%s",
            drive_file.file_id,
            drive_file.mime_type,
        )
        return ""


def _extract_via_notes_ingestor(*, drive_client, extractor, drive_file) -> str:
    """Download bytes via the Drive adapter, dispatch to the multimodal extractor.

    The notes_ingestor's ``GeminiMultimodalExtractor.extract`` takes
    ``data: bytes``, ``mime_type``, ``file_name`` — we download first,
    then hand bytes off. Markdown / text fast-path skips the LLM
    entirely and just decodes the bytes directly.
    """
    mime = (drive_file.mime_type or "").lower()

    # Google Docs need export-as-markdown; everything else downloads raw bytes.
    try:
        if mime == "application/vnd.google-apps.document":
            blob = drive_client.export_doc_as_markdown(drive_file.file_id)
        else:
            blob = drive_client.download_file(drive_file.file_id)
    except Exception:
        log.exception("librarian.download_failed file=%s mime=%s", drive_file.file_id, mime)
        return ""

    if not blob:
        return ""

    # Plain-text + markdown fast-path: skip the LLM, just decode.
    if mime in {"text/markdown", "text/plain"}:
        try:
            return blob.decode("utf-8", errors="replace")
        except Exception:
            log.exception("librarian.decode_failed file=%s", drive_file.file_id)
            return ""

    try:
        result = extractor.extract(
            data=blob,
            mime_type=drive_file.mime_type or "",
            file_name=drive_file.name,
        )
    except Exception:
        log.exception("librarian.extract_failed file=%s mime=%s", drive_file.file_id, mime)
        return ""
    return getattr(result, "markdown", None) or ""


def _find_dossier_doc_id(*, drive_client, folder_id: str, dossier_filename: str) -> str | None:
    if not dossier_filename:
        return None
    try:
        rows = drive_client.list_files(
            folder_id=folder_id,
            page_size=20,
            fields="files(id, name, mimeType)",
        )
    except Exception:
        log.exception("librarian.dossier_lookup_failed folder=%s", folder_id)
        return None
    target_lc = dossier_filename.lower()
    for r in rows:
        name = str(r.get("name") or "").lower()
        # Tolerate users naming the dossier "dossier" (no extension)
        # OR "<topic>-dossier.gdoc" — match either contains-name.
        if name == target_lc or "dossier" in name:
            file_id = r.get("id")
            if file_id:
                return str(file_id)
    return None


# ---------------------------------------------------------------------------
# small helpers + adapters
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("librarian.bad_int_env name=%s value=%r — using default %d", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "librarian.bad_float_env name=%s value=%r — using default %s", name, raw, default
        )
        return default


class _ParameterizedBQAdapter:
    """Adapter for the linker's ``BQQueryClient`` Protocol.

    Mirrors evening_reflection.main._BigQueryParameterizedAdapter exactly.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery as bq_module

        params = []
        for p in parameters or []:
            ptype = p.get("type", "STRING")
            value = p["value"]
            name = p["name"]
            if ptype.startswith("ARRAY_"):
                element_type = ptype[len("ARRAY_") :]
                params.append(bq_module.ArrayQueryParameter(name, element_type, list(value)))
            else:
                params.append(bq_module.ScalarQueryParameter(name, ptype, value))
        job_config = bq_module.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


class _InsertBQAdapter:
    """Adapter from ``google.cloud.bigquery.Client`` to insert_rows_json shape."""

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._bq.insert_rows_json(table_ref, rows)


def _load_processed_file_ids(
    bq_client: Any, *, project_id: str, lookback_days: int = 30
) -> set[str]:
    """Return the set of Drive file_ids the Librarian has already moved.

    Reads ``agent_audit_log.events`` for librarian file_outcome rows
    with ``moved=true`` over the last ``lookback_days`` days. Used by
    the lister to skip re-processing files whose originals stayed in
    Drop after a copy-fallback (the SA can't delete files it doesn't
    own; the user removes them manually after verifying the move).

    Rows whose ingest failed (``ingest_error`` set) are deliberately
    NOT skipped: the corpus write never happened, the original was kept
    in Drop (archive is gated on ingest success), and the next tick
    must retry. ``ingest_error IS NULL`` also covers pre-Phase-G rows
    that lack the ingest_* keys entirely.
    """
    sql = f"""
SELECT DISTINCT JSON_VALUE(output, '$.file_id') AS file_id
FROM `{project_id}.agent_audit_log.events`
WHERE agent_id = 'librarian'
  AND JSON_VALUE(output, '$.event_kind') = 'file_outcome'
  AND JSON_VALUE(output, '$.moved') = 'true'
  AND JSON_VALUE(output, '$.ingest_error') IS NULL
  AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_days} DAY)
"""  # — lookback is int, no user input
    try:
        rows = list(bq_client.query(sql).result())
    except Exception:
        log.exception("librarian.load_processed_file_ids_failed")
        return set()
    return {str(r["file_id"]) for r in rows if r.get("file_id")}


class _DocsServiceFactory:
    """Builds a Docs v1 service via ADC.

    Mirrors notes_ingestor.drive_client.ADCDriveServiceFactory but for
    the Docs API. Required scope is ``documents`` (read/write).
    """

    def __init__(self, *, scope: str) -> None:
        self._scope = scope

    def build(self) -> Any:
        from google.auth import default
        from googleapiclient.discovery import build

        creds, _ = default(scopes=[self._scope])
        return build("docs", "v1", credentials=creds, cache_discovery=False)


class _DriveAdapter:
    """Minimal Drive v3 wrapper used by the Librarian's lister + extractor.

    The notes_ingestor's ``NotesDriveClient`` is shaped around watermark/
    kind-aware ingestion. The Librarian wants a raw ``files.list`` over
    a folder + ``files.get_media`` for downloads. This adapter supplies
    that surface against an already-built googleapiclient Drive service.

    Methods:
      - ``list_files(folder_id, page_size, fields)`` →
        ``files.list(q="'<folder>' in parents and trashed=false", ...)``
        returning ``[{id, name, mimeType, parents, modifiedTime, webViewLink}, ...]``.
      - ``download_file(file_id) -> bytes`` via ``files.get_media``.
      - ``export_doc_as_markdown(file_id) -> bytes`` via ``files.export``
        with ``mimeType=text/markdown`` (matches the notes_ingestor
        Google-Doc export pattern in ``extractor.py``).
    """

    def __init__(self, service: Any) -> None:
        self._svc = service

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]:
        from googleapiclient.errors import HttpError

        # ``q=`` requires the folder id to be quoted; parent membership
        # checked via ``in parents``. trashed=false excludes deleted items.
        q = f"'{folder_id}' in parents and trashed = false"
        try:
            response = (
                self._svc.files()
                .list(
                    q=q,
                    pageSize=page_size,
                    fields=fields,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError:
            log.exception("librarian.drive.list_failed folder=%s", folder_id)
            raise
        return list(response.get("files", []) or [])

    def download_file(self, file_id: str) -> bytes:
        import io

        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        try:
            request = self._svc.files().get_media(fileId=file_id, supportsAllDrives=True)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue()
        except HttpError:
            log.exception("librarian.drive.download_failed file=%s", file_id)
            raise

    def export_doc_as_markdown(self, file_id: str) -> bytes:
        import io

        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        try:
            request = self._svc.files().export_media(fileId=file_id, mimeType="text/markdown")
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue()
        except HttpError:
            log.exception("librarian.drive.export_failed file=%s", file_id)
            raise


if __name__ == "__main__":
    sys.exit(main())
