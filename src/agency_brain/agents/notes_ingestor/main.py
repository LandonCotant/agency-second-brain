"""Cloud Run Job entrypoint for the notes ingestor.

Originally ADR 0031 (Samsung Notes — PDF only). ADR 0037 / 0038 extend
it to ingest from the IPARAG-adapted ``Brain/`` Drive layout (PDF +
Markdown + Google Docs + audio) and embed every row via Vertex
``text-embedding-005``.

One execution per scheduler tick. Per folder:

  1. Read watermark.
  2. List files matching the supported MIME / suffix allowlist newer
     than the watermark (cap at MAX_NOTES_PER_TICK).
  3. For each file:
     a. Pre-INSERT SELECT on (file_id, revision_id). Hit → skip.
     b. Download via ``drive.download_file`` (Google Docs go through
        ``export_media`` → text/markdown).
     c. Extract Markdown via ``GeminiMultimodalExtractor`` (PDF → LLM,
        audio → LLM, MD/Doc → passthrough).
     d. Embed the Markdown via ``embed_markdown`` (text-embedding-005,
        ADR 0038 §1).
     e. Write a row to ``agent_outputs.notes`` with new
        ``note_kind`` / ``scope`` / ``embedding`` columns populated.
     f. Publish to ``asb-triage-input`` ONLY when the folder's
        ``note_kind`` is ``inbox`` (ADR 0037 §6).
     g. Move the file to the folder's ``processed/`` subfolder
        (best effort).
     h. Emit one ``agent_audit_log.events`` row per file.
  4. Advance the watermark to the highest modifiedTime processed.

Env vars (set in
``terraform/modules/agent_runtime/notes_ingestor.tf``):

  BRAIN_PROJECT_ID                       GCP project id
  NOTES_FOLDER_ID                        legacy Samsung-Notes lane (ADR 0031)
  NOTES_HIPAA_FOLDER_ID                  Brain/Inbox/HIPAA/ (ADR 0031 + 0037)
  BRAIN_INBOX_VOICE_FOLDER_ID            Brain/Inbox/Voice/    (optional)
  BRAIN_INBOX_QUICKNOTES_FOLDER_ID       Brain/Inbox/QuickNotes/ (optional)
  BRAIN_INBOX_READING_FOLDER_ID          Brain/Inbox/Reading/  (optional)
  BRAIN_AREAS_FOLDER_ID                  Brain/Areas/          (optional)
  BRAIN_RESOURCES_FOLDER_ID              Brain/Resources/      (optional)
  BRAIN_ARCHIVES_FOLDER_ID               Brain/Archives/       (optional)
  SOLUTIONS_CLIENTS_FOLDER_ID            Solutions/05_CLIENTS/ (ADR 0048 §3, optional)
  SOLUTIONS_MANAGEMENT_LEGAL_FOLDER_ID   Solutions/01_MANAGEMENT & LEGAL/ (ADR 0048 §2)
  SOLUTIONS_FINANCE_FOLDER_ID            Solutions/02_FINANCE & ACCOUNTING/ (ADR 0048 §2)
  SOLUTIONS_OPERATIONS_HR_FOLDER_ID      Solutions/03_OPERATIONS & HR/ (ADR 0048 §2)
  SOLUTIONS_SALES_MARKETING_FOLDER_ID    Solutions/04_SALES & MARKETING (Internal)/ (ADR 0048 §2)
  NOTES_INGESTOR_SA_EMAIL                for the audit row's sa_email column
  MAX_NOTES_PER_TICK                     bound (default 100)
  VERTEX_LOCATION                        default us-central1
  EMBEDDING_MODEL                        default text-embedding-005

Optional folders silently skip when the env var is unset — supports
gradual rollout where the Drive folders may not yet exist.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .embedder import DEFAULT_MODEL as DEFAULT_EMBED_MODEL
from .embedder import EmbeddingResult, EmbedError, embed_markdown
from .models import (
    DriveFile,
    ExtractionMethod,
    ExtractionResult,
    IngestOutcome,
    IngestSummary,
    NoteFolder,
    NoteRow,
    is_hipaa_folder,
    note_kind_for_folder,
    scope_for_folder,
    should_move_to_processed,
)

log = logging.getLogger("agency_brain.agents.notes_ingestor.main")

# Env-var → folder-role map. Each entry is optional in production —
# unset env vars silently skip. Order matters only for the watermark
# advance: each folder runs serially.
_FOLDER_ENV_MAP: tuple[tuple[str, NoteFolder], ...] = (
    ("NOTES_FOLDER_ID", NoteFolder.DEFAULT),
    ("NOTES_HIPAA_FOLDER_ID", NoteFolder.HIPAA),
    ("BRAIN_INBOX_VOICE_FOLDER_ID", NoteFolder.INBOX_VOICE),
    ("BRAIN_INBOX_QUICKNOTES_FOLDER_ID", NoteFolder.INBOX_QUICKNOTES),
    ("BRAIN_INBOX_READING_FOLDER_ID", NoteFolder.INBOX_READING),
    ("BRAIN_AREAS_FOLDER_ID", NoteFolder.AREAS),
    ("BRAIN_RESOURCES_FOLDER_ID", NoteFolder.RESOURCES),
    ("BRAIN_ARCHIVES_FOLDER_ID", NoteFolder.ARCHIVES),
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"severity":"%(levelname)s","name":"%(name)s","message":%(message)r}',
    )

    project_id = os.environ["BRAIN_PROJECT_ID"]
    sa_email = os.environ.get(
        "NOTES_INGESTOR_SA_EMAIL",
        f"asb-notes-ingestor-sa@{project_id}.iam.gserviceaccount.com",
    )
    max_per_tick = int(os.environ.get("MAX_NOTES_PER_TICK", "100"))
    vertex_location = os.environ.get("VERTEX_LOCATION", "us-central1")
    embedding_model = os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBED_MODEL)
    backfill_mode = os.environ.get("BACKFILL_MODE", "none").strip().lower()
    max_backfill_per_tick = int(os.environ.get("MAX_BACKFILL_PER_TICK", "100"))

    # Lazy imports — Cloud Run Jobs cold-start cost grows with import time.
    from google.cloud import bigquery, pubsub_v1

    from ...common.audit_log import AuditLogClient
    from .drive_client import (
        ADCDriveServiceFactory,
        FolderConfig,
        NotesDriveClient,
    )
    from .embedder import VertexEmbedder
    from .extractor import (
        DEFAULT_MODEL as DEFAULT_EXTRACT_MODEL,
    )
    from .extractor import (
        GeminiMultimodalExtractor,
        VertexMultimodalLLM,
    )
    from .triage_publisher import TriagePublisher
    from .writer import NotesWriter, WatermarkStore

    bq = bigquery.Client(project=project_id)
    bq_rows = _BQRowsAdapter(bq)
    bq_query = _BQQueryAdapter(bq)
    bq_update = _BQUpdateAdapter(bq)
    audit = AuditLogClient(project_id=project_id, bq_client=bq)

    drive = NotesDriveClient(service_factory=ADCDriveServiceFactory())
    extractor = GeminiMultimodalExtractor(
        llm=VertexMultimodalLLM(project_id=project_id, location=vertex_location),
        model=DEFAULT_EXTRACT_MODEL,
    )
    embedder = VertexEmbedder(project_id=project_id, location=vertex_location)
    notes_writer = NotesWriter(
        bq_rows=bq_rows,
        bq_query=bq_query,
        bq_update=bq_update,
        project_id=project_id,
    )
    watermark = WatermarkStore(bq_rows=bq_rows, bq_query=bq_query, project_id=project_id)
    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
    )
    triage_publisher = TriagePublisher(publisher=publisher, project_id=project_id)

    # ADR 0039 §4 — env-var-gated backfill mode. ``embeddings_only`` skips
    # the folder loop entirely and runs the corpus-wide embedding backfill
    # for pre-ADR-0038 rows.
    if backfill_mode == "embeddings_only":
        log.info("notes_ingestor.backfill_mode_start max=%d", max_backfill_per_tick)
        backfill_summary = _run_embeddings_backfill(
            notes_writer=notes_writer,
            embedder=embedder,
            embedding_model=embedding_model,
            audit=audit,
            sa_email=sa_email,
            max_per_tick=max_backfill_per_tick,
        )
        log.info(
            json.dumps(
                {
                    "event": "NOTES_BACKFILL_DONE",
                    "scanned": backfill_summary["scanned"],
                    "embedded": backfill_summary["embedded"],
                    "updated": backfill_summary["updated"],
                    "failures": backfill_summary["failures"],
                }
            )
        )
        return 0 if backfill_summary["failures"] == 0 else 1

    folders: list[FolderConfig] = []
    for env_var, role in _FOLDER_ENV_MAP:
        folder_id = os.environ.get(env_var, "").strip()
        if folder_id:
            folders.append(FolderConfig(folder_id=folder_id, role=role))

    # ADR 0048 — Solutions Shared Drive sweep. Expansion happens at tick
    # start so HIPAA filtering reads the current Airtable replica state
    # (each tick gets the latest accounts.hipaa values).
    folders.extend(_build_solutions_folders(drive=drive, bq_query=bq_query, project_id=project_id))

    if not folders:
        log.error(
            "notes_ingestor.no_folders_configured — set at least one of "
            "NOTES_FOLDER_ID, NOTES_HIPAA_FOLDER_ID, any BRAIN_*_FOLDER_ID "
            "env var, or any SOLUTIONS_*_FOLDER_ID env var."
        )
        return 1

    summary = run_tick(
        folders=folders,
        drive=drive,
        extractor=extractor,
        embedder=embedder,
        embedding_model=embedding_model,
        notes_writer=notes_writer,
        triage_publisher=triage_publisher,
        watermark=watermark,
        audit=audit,
        sa_email=sa_email,
        max_per_tick=max_per_tick,
    )

    log.info(
        json.dumps(
            {
                "event": "NOTES_INGEST_DONE",
                "listed": summary.listed,
                "extracted": summary.extracted,
                "written": summary.written,
                "triage_published": summary.triage_published,
                "dedup_skipped": summary.dedup_skipped,
                "failures": summary.failures,
            }
        )
    )
    return 0 if summary.failures == 0 else 1


def run_tick(
    *,
    folders: list,
    drive: Any,
    extractor: Any,
    embedder: Any,
    embedding_model: str,
    notes_writer: Any,
    triage_publisher: Any,
    watermark: Any,
    audit: Any,
    sa_email: str,
    max_per_tick: int,
) -> IngestSummary:
    """Process every watched folder; return a summary for tests + logging.

    Each folder is independent: a failure on one folder does not abort
    the others. Per-file failures are caught and recorded as audit rows
    + ``IngestOutcome(error=...)``.
    """
    summary = IngestSummary()
    remaining_budget = max_per_tick

    for folder in folders:
        if remaining_budget <= 0:
            break

        try:
            since = watermark.read(folder.folder_id)
        except Exception:
            log.exception(
                "notes_ingestor.watermark_read_failed folder=%s",
                folder.folder_id,
            )
            summary.failures += 1
            continue

        try:
            files = list(drive.list_new_files(folder=folder, since=since))
        except Exception:
            log.exception("notes_ingestor.list_failed folder=%s", folder.folder_id)
            summary.failures += 1
            continue

        # Sorted ascending by modifiedTime so the watermark advances
        # correctly — Drive `orderBy=modifiedTime asc` already guarantees
        # this, but we don't trust the API to be stable forever.
        files.sort(key=lambda f: f.modified_time)
        files = files[:remaining_budget]
        summary.listed += len(files)

        max_modified_seen = since
        for f in files:
            outcome = _process_file(
                drive_file=f,
                folder=folder,
                drive=drive,
                extractor=extractor,
                embedder=embedder,
                embedding_model=embedding_model,
                notes_writer=notes_writer,
                triage_publisher=triage_publisher,
                audit=audit,
                sa_email=sa_email,
            )
            summary.folder_outcomes.append(outcome)
            if outcome.error:
                summary.failures += 1
            elif outcome.ingested:
                summary.extracted += 1
                summary.written += 1
                if outcome.triage_published:
                    summary.triage_published += 1
            else:
                summary.dedup_skipped += 1

            # Advance the watermark candidate even on dedup-skip — the
            # row already exists, so we never want to re-list this file.
            # On error we leave the watermark untouched so the next tick
            # retries.
            if not outcome.error:
                if max_modified_seen is None or f.modified_time > max_modified_seen:
                    max_modified_seen = f.modified_time

            remaining_budget -= 1
            if remaining_budget <= 0:
                break

        if max_modified_seen is not None and max_modified_seen != since:
            try:
                watermark.write(
                    folder_path=folder.folder_id,
                    last_modified_time_seen=max_modified_seen,
                )
            except Exception:
                log.exception(
                    "notes_ingestor.watermark_write_failed folder=%s",
                    folder.folder_id,
                )
                summary.failures += 1

    return summary


def _process_file(
    *,
    drive_file: DriveFile,
    folder: Any,
    drive: Any,
    extractor: Any,
    embedder: Any,
    embedding_model: str,
    notes_writer: Any,
    triage_publisher: Any,
    audit: Any,
    sa_email: str,
) -> IngestOutcome:
    """One file → one audit row + one (or zero) BQ row + one (or zero) Pub/Sub publish."""
    started = time.perf_counter()
    triage_published = False
    extraction: ExtractionResult | None = None

    try:
        existing = notes_writer.find_existing(
            source_drive_file_id=drive_file.file_id,
            revision_id=drive_file.revision_id,
        )
        if existing is not None:
            log.info(
                "notes_ingestor.dedup_skip file_id=%s revision_id=%s existing=%s",
                drive_file.file_id,
                drive_file.revision_id,
                existing.note_id,
            )
            _emit_audit(
                audit=audit,
                sa_email=sa_email,
                drive_file=drive_file,
                started=started,
                outcome_payload={
                    "decision": "dedup_skipped",
                    "existing_note_id": existing.note_id,
                    "folder": folder.role.value,
                },
                success=True,
            )
            # ADR 0048 §4 — Solutions reference files stay where they are.
            if should_move_to_processed(folder.role):
                _safe_move(drive=drive, drive_file=drive_file, folder=folder)
            return IngestOutcome(
                file_id=drive_file.file_id,
                revision_id=drive_file.revision_id,
                folder=folder.role,
                ingested=False,
                triage_published=False,
                extraction_method=ExtractionMethod.GEMINI_FLASH,
                page_count=0,
            )

        data = drive.download_file(file_id=drive_file.file_id, mime_type=drive_file.mime_type)
        extraction = extractor.extract(
            data=data,
            mime_type=drive_file.mime_type,
            file_name=drive_file.name,
        )

        # ADR 0048 — for Solutions Drive files, prepend a header carrying
        # the client name + source path so the embedding (and downstream
        # /ask retrieval) sees the client tag.
        markdown_for_row = _maybe_prepend_source_header(
            markdown=extraction.markdown,
            folder=folder,
            file_name=drive_file.name,
        )

        # Embedding step (ADR 0038 §4). Failure here downgrades the row
        # to "no embedding" but doesn't drop the corpus row — Phase 5
        # Connector / VECTOR_SEARCH consumers filter on
        # ``embedding IS NOT NULL`` (or check ARRAY_LENGTH) anyway.
        embedding: EmbeddingResult | None = None
        try:
            embedding = embed_markdown(
                markdown=markdown_for_row,
                embedder=embedder,
                model=embedding_model,
            )
        except EmbedError:
            log.exception(
                "notes_ingestor.embed_failed file_id=%s — landing row without embedding",
                drive_file.file_id,
            )

        row = NoteRow(
            note_id=drive_file.file_id,
            revision_id=drive_file.revision_id,
            ingested_at=datetime.now(UTC),
            created_at=drive_file.modified_time,
            source_drive_file_id=drive_file.file_id,
            source_drive_url=drive_file.web_view_link,
            filename=drive_file.name,
            markdown_content=markdown_for_row,  # ADR 0048 — Solutions header prepended pre-embed
            extraction_method=extraction.method,
            extraction_confidence=extraction.confidence,
            extraction_notes=extraction.notes,
            page_count=extraction.page_count,
            hipaa_isolated=is_hipaa_folder(folder.role),
            # ADR 0037 / 0038 — PKM merge fields
            note_kind=note_kind_for_folder(folder.role),
            scope=scope_for_folder(folder.role),
            embedding_content_hash=(embedding.content_hash if embedding else None),
            embedding=(embedding.vector if embedding else ()),
            embedding_model=(embedding.model if embedding and embedding.vector else None),
            embedding_generated_at=(datetime.now(UTC) if embedding and embedding.vector else None),
        )
        notes_writer.write(row)

        # ADR 0037 §6 — kind-gated publish. INBOX folders publish to
        # triage; reference folders (Areas/Resources/Archives) don't.
        # The publisher returns ``None`` (not an exception) when the
        # kind doesn't trigger publish — distinct from a publish error.
        try:
            msg_id = triage_publisher.publish(drive_file=drive_file, extraction=extraction)
            triage_published = msg_id is not None
        except Exception:
            log.exception(
                "notes_ingestor.publish_failed file_id=%s",
                drive_file.file_id,
            )
            # Still count as ingested — the corpus row landed; downstream
            # retries can republish manually.

        # ADR 0048 §4 — Solutions reference files stay where they are.
        if should_move_to_processed(folder.role):
            _safe_move(drive=drive, drive_file=drive_file, folder=folder)

        _emit_audit(
            audit=audit,
            sa_email=sa_email,
            drive_file=drive_file,
            started=started,
            outcome_payload={
                "decision": "ingested",
                "folder": folder.role.value,
                "mime_type": drive_file.mime_type,
                "note_kind": note_kind_for_folder(folder.role).value,
                "extraction_method": extraction.method.value,
                "extraction_confidence": extraction.confidence,
                "page_count": extraction.page_count,
                "triage_published": triage_published,
                "hipaa_isolated": is_hipaa_folder(folder.role),
                "embedding_dim": (len(embedding.vector) if embedding else 0),
            },
            success=True,
        )
        return IngestOutcome(
            file_id=drive_file.file_id,
            revision_id=drive_file.revision_id,
            folder=folder.role,
            ingested=True,
            triage_published=triage_published,
            extraction_method=extraction.method,
            page_count=extraction.page_count,
        )

    except Exception as exc:  # pragma: no cover — exercised in test_main
        log.exception("notes_ingestor.file_failed file_id=%s", drive_file.file_id)
        _emit_audit(
            audit=audit,
            sa_email=sa_email,
            drive_file=drive_file,
            started=started,
            outcome_payload={
                "decision": "failed",
                "folder": folder.role.value,
                "error": f"{type(exc).__name__}: {exc}",
            },
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        method = extraction.method if extraction is not None else ExtractionMethod.FAILED
        page_count = extraction.page_count if extraction is not None else 0
        return IngestOutcome(
            file_id=drive_file.file_id,
            revision_id=drive_file.revision_id,
            folder=folder.role,
            ingested=False,
            triage_published=False,
            extraction_method=method,
            page_count=page_count,
            error=f"{type(exc).__name__}: {exc}",
        )


def _safe_move(*, drive: Any, drive_file: DriveFile, folder: Any) -> None:
    try:
        drive.move_to_processed(
            file_id=drive_file.file_id,
            current_parent=folder.folder_id,
            folder=folder,
        )
    except Exception:
        log.exception("notes_ingestor.move_failed file_id=%s", drive_file.file_id)


# ----------------------------------------------------- Solutions sweep helpers (ADR 0048)


_SOLUTIONS_INTERNAL_ENV_MAP: tuple[tuple[str, str], ...] = (
    ("SOLUTIONS_MANAGEMENT_LEGAL_FOLDER_ID", "01_MANAGEMENT & LEGAL"),
    ("SOLUTIONS_FINANCE_FOLDER_ID", "02_FINANCE & ACCOUNTING"),
    ("SOLUTIONS_OPERATIONS_HR_FOLDER_ID", "03_OPERATIONS & HR"),
    ("SOLUTIONS_SALES_MARKETING_FOLDER_ID", "04_SALES & MARKETING (Internal)"),
)


def _build_solutions_folders(*, drive: Any, bq_query: Any, project_id: str) -> list[Any]:
    """ADR 0048 — expand Solutions Drive env vars into FolderConfigs.

    Returns at most:
      - N FolderConfigs for clients (one per allowlisted subfolder under
        each non-HIPAA, non-template client) when ``SOLUTIONS_CLIENTS_FOLDER_ID``
        is set.
      - One recursive FolderConfig per top-level internal folder env var.

    All unset env vars silently skip. A failure to load HIPAA accounts
    fails closed: the client sweep is skipped entirely for that tick
    (logged as ERROR) so HIPAA content never reaches the corpus.
    """
    from .solutions_discovery import (
        expand_solutions_clients,
        internal_folder_config,
        load_hipaa_account_names,
    )

    out: list[Any] = []

    clients_root = os.environ.get("SOLUTIONS_CLIENTS_FOLDER_ID", "").strip()
    if clients_root:
        try:
            hipaa_names = load_hipaa_account_names(bq_query, project_id=project_id)
        except Exception:
            log.exception(
                "notes_ingestor.hipaa_load_failed — skipping Solutions client sweep this tick"
            )
        else:
            client_configs = expand_solutions_clients(
                drive,
                clients_root_id=clients_root,
                hipaa_account_names=hipaa_names,
            )
            out.extend(client_configs)
            log.info(
                "notes_ingestor.solutions_clients_built n_hipaa=%d n_configs=%d",
                len(hipaa_names),
                len(client_configs),
            )

    for env_var, source_path in _SOLUTIONS_INTERNAL_ENV_MAP:
        folder_id = os.environ.get(env_var, "").strip()
        if not folder_id:
            continue
        out.append(internal_folder_config(folder_id=folder_id, source_path=source_path))

    return out


def _maybe_prepend_source_header(*, markdown: str, folder: Any, file_name: str) -> str:
    """ADR 0048 §5 — for Solutions-Drive files, prepend a 1-3 line header
    so the embedding (and Gemini /ask synthesis) sees the client name +
    source path. No-op for Brain folders (preserves existing corpus shape).

    Header shape::

        Client: Client A
        Source: 05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES/<file_name>

        <original markdown>

    Files without ``client_name`` (internal folders) get only the
    ``Source:`` line.
    """
    lines: list[str] = []
    client_name = getattr(folder, "client_name", None)
    source_path = getattr(folder, "source_path", None)
    if client_name:
        lines.append(f"Client: {client_name}")
    if source_path:
        lines.append(f"Source: {source_path}/{file_name}")
    if not lines:
        return markdown
    header = "\n".join(lines) + "\n\n"
    return header + (markdown or "")


def _emit_audit(
    *,
    audit: Any,
    sa_email: str,
    drive_file: DriveFile,
    started: float,
    outcome_payload: dict,
    success: bool,
    error: str | None = None,
) -> None:
    """Mirror BaseAgent's audit shape but emit directly (we are not a BaseAgent)."""
    from ...common.models import AuditEvent, HipaaGuardStatus

    latency_ms = int((time.perf_counter() - started) * 1000)
    input_summary = json.dumps(
        {
            "source": "drive",
            "file_id": drive_file.file_id,
            "revision_id": drive_file.revision_id,
            "filename": drive_file.name,
            "mime_type": drive_file.mime_type,
        }
    )
    event = AuditEvent(
        event_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        agent_id="notes-ingestor",
        sa_email=sa_email,
        latency_ms=latency_ms,
        # The ingestor itself never reads HIPAA-cascade content — the
        # HIPAA aspect rides on the triage publish and the BaseAgent
        # short-circuit downstream is what actually enforces isolation.
        # PASSED here means "ingestor's own boundaries were respected".
        hipaa_guard_status=HipaaGuardStatus.PASSED,
        success=success,
        human_review_routed=False,
        input_summary=input_summary,
        output=json.dumps(outcome_payload),
        error=error,
    )
    try:
        audit.emit(event)
    except Exception:
        # Audit failures must not silently drop, but they also must not
        # cause us to lose the BQ row that already landed. Log loudly
        # and move on — PRD §8.2 alert on audit-write failures will
        # surface this in monitoring.
        log.exception("notes_ingestor.audit_emit_failed event_id=%s", event.event_id)


# ----------------------------------------------------- Backfill mode (ADR 0039 §4)


def _run_embeddings_backfill(
    *,
    notes_writer: Any,
    embedder: Any,
    embedding_model: str,
    audit: Any,
    sa_email: str,
    max_per_tick: int,
) -> dict[str, int]:
    """One-shot embeddings backfill for pre-ADR-0038 rows.

    Reads up to ``max_per_tick`` rows from ``agent_outputs.notes`` where
    ``embedding IS NULL OR ARRAY_LENGTH(embedding) = 0``, embeds each
    row's ``markdown_content``, and UPDATEs the embedding columns.

    Triggered by ``BACKFILL_MODE=embeddings_only`` (ADR 0039 §4). Once
    the corpus is fully embedded, unset the env var; the next scheduled
    tick runs the normal folder loop.
    """
    started_tick = time.perf_counter()
    rows = notes_writer.find_unembedded_rows(limit=max_per_tick)
    scanned = len(rows)
    embedded = 0
    updated = 0
    failures = 0

    for row in rows:
        note_id = row.get("note_id")
        revision_id = row.get("revision_id")
        markdown = row.get("markdown_content") or ""
        if not note_id or not revision_id:
            failures += 1
            continue

        per_row_started = time.perf_counter()
        try:
            embedding = embed_markdown(
                markdown=markdown,
                embedder=embedder,
                model=embedding_model,
            )
            embedded += 1
        except EmbedError:
            log.exception("notes_ingestor.backfill.embed_failed note_id=%s", note_id)
            failures += 1
            _emit_backfill_audit(
                audit=audit,
                sa_email=sa_email,
                note_id=note_id,
                revision_id=revision_id,
                started=per_row_started,
                success=False,
                error="embed_failed",
            )
            continue

        try:
            notes_writer.update_embedding(
                note_id=note_id,
                revision_id=revision_id,
                embedding=list(embedding.vector),
                model=embedding.model,
                content_hash=embedding.content_hash,
                generated_at=datetime.now(UTC),
            )
            updated += 1
        except Exception as exc:
            log.exception("notes_ingestor.backfill.update_failed note_id=%s", note_id)
            failures += 1
            _emit_backfill_audit(
                audit=audit,
                sa_email=sa_email,
                note_id=note_id,
                revision_id=revision_id,
                started=per_row_started,
                success=False,
                error=f"update_failed: {type(exc).__name__}: {exc}",
            )
            continue

        _emit_backfill_audit(
            audit=audit,
            sa_email=sa_email,
            note_id=note_id,
            revision_id=revision_id,
            started=per_row_started,
            success=True,
            embedding_dim=len(embedding.vector),
        )

    log.info(
        "notes_ingestor.backfill.tick_done scanned=%d embedded=%d updated=%d failures=%d latency_ms=%d",
        scanned,
        embedded,
        updated,
        failures,
        int((time.perf_counter() - started_tick) * 1000),
    )
    return {
        "scanned": scanned,
        "embedded": embedded,
        "updated": updated,
        "failures": failures,
    }


def _emit_backfill_audit(
    *,
    audit: Any,
    sa_email: str,
    note_id: str,
    revision_id: str,
    started: float,
    success: bool,
    error: str | None = None,
    embedding_dim: int | None = None,
) -> None:
    from ...common.models import AuditEvent, HipaaGuardStatus

    latency_ms = int((time.perf_counter() - started) * 1000)
    payload: dict = {
        "decision": "backfill_embedded" if success else "backfill_failed",
        "note_id": note_id,
        "revision_id": revision_id,
    }
    if embedding_dim is not None:
        payload["embedding_dim"] = embedding_dim
    event = AuditEvent(
        event_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        agent_id="notes-ingestor",
        sa_email=sa_email,
        latency_ms=latency_ms,
        hipaa_guard_status=HipaaGuardStatus.PASSED,
        success=success,
        human_review_routed=False,
        input_summary=json.dumps({"backfill": True, "note_id": note_id}),
        output=json.dumps(payload),
        error=error,
    )
    try:
        audit.emit(event)
    except Exception:
        log.exception(
            "notes_ingestor.backfill.audit_emit_failed event_id=%s",
            event.event_id,
        )


# ----------------------------------------------------- BigQuery adapters


class _BQRowsAdapter:
    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        return self._bq.insert_rows_json(table_ref, rows)


class _BQQueryAdapter:
    """Parameterized SELECT helper.

    Translates the writer-facing ``parameters`` list (with `name`,
    `type`, `value` keys) into ``bigquery.ScalarQueryParameter`` objects
    so the writer module never imports ``google.cloud.bigquery``
    directly — keeping unit tests free of the SDK.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        from google.cloud import bigquery

        params = [
            bigquery.ScalarQueryParameter(p["name"], p["type"], p["value"])
            for p in (parameters or [])
        ]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in self._bq.query(sql, job_config=job_config).result()]


class _BQUpdateAdapter:
    """Parameterized UPDATE helper for the embeddings backfill (ADR 0039 §4).

    Returns the number of rows affected. Translates ``parameters`` (with
    optional ``mode='REPEATED'``) into ``ScalarQueryParameter`` or
    ``ArrayQueryParameter`` depending on the field's mode.
    """

    def __init__(self, bq_client: Any) -> None:
        self._bq = bq_client

    def update_rows(self, sql: str, parameters: list[dict] | None = None) -> int:
        from google.cloud import bigquery

        params: list = []
        for p in parameters or []:
            if p.get("mode") == "REPEATED":
                params.append(bigquery.ArrayQueryParameter(p["name"], p["type"], p["value"]))
            else:
                params.append(bigquery.ScalarQueryParameter(p["name"], p["type"], p["value"]))
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        job = self._bq.query(sql, job_config=job_config)
        job.result()  # block until the UPDATE completes
        # ``num_dml_affected_rows`` is set on completion of a DML job.
        return int(getattr(job, "num_dml_affected_rows", 0) or 0)


if __name__ == "__main__":
    sys.exit(main())
