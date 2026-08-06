"""End-to-end test of the notes ingestor `run_tick` orchestration with fakes.

ADR 0031 covered: fresh ingest → write → publish → move; dedup hit →
skip; publish failure / list failure isolation; max-per-tick budget;
audit failure non-fatal.

ADR 0037 / 0038 add: multi-MIME drive listing (`list_new_files`),
multi-MIME extraction (`extract(data, mime_type)`), embedder integration
(NoteRow gets `embedding`, `embedding_content_hash`, etc.), kind-gated
triage publish (reference folders skip publishing without raising).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.notes_ingestor.drive_client import FolderConfig
from agency_brain.agents.notes_ingestor.main import run_tick
from agency_brain.agents.notes_ingestor.models import (
    DriveFile,
    ExtractionMethod,
    ExtractionResult,
    NoteFolder,
    NoteKind,
    Scope,
)
from agency_brain.agents.notes_ingestor.writer import DedupHit

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDrive:
    files_by_folder: dict[str, list[DriveFile]] = field(default_factory=dict)
    downloads: dict[str, bytes] = field(default_factory=dict)
    moved: list[str] = field(default_factory=list)
    move_raises: BaseException | None = None
    list_raises: BaseException | None = None

    def list_new_files(
        self,
        *,
        folder: FolderConfig,
        since: datetime | None,
        mime_types: Any = None,
        name_suffixes: Any = None,
    ):
        if self.list_raises is not None:
            raise self.list_raises
        for f in self.files_by_folder.get(folder.folder_id, []):
            if since is None or f.modified_time > since:
                yield f

    def download_file(self, *, file_id: str, mime_type: str) -> bytes:
        return self.downloads.get(file_id, b"%PDF-fake")

    def move_to_processed(self, *, file_id: str, current_parent: str, folder: FolderConfig) -> str:
        if self.move_raises is not None:
            raise self.move_raises
        self.moved.append(file_id)
        return f"{current_parent}/processed"


@dataclass
class _FakeExtractor:
    return_values: dict[str, ExtractionResult] = field(default_factory=dict)
    default: ExtractionResult = field(
        default_factory=lambda: ExtractionResult(
            markdown="# default\nbody",
            page_count=1,
            method=ExtractionMethod.GEMINI_FLASH,
            confidence=0.9,
        )
    )
    captured_mimes: list[str] = field(default_factory=list)

    def extract(self, *, data: bytes, mime_type: str, file_name: str = "") -> ExtractionResult:
        self.captured_mimes.append(mime_type)
        for key, val in self.return_values.items():
            if key in data.decode("utf-8", errors="ignore"):
                return val
        return self.default


@dataclass
class _FakeEmbedder:
    """Mirror of the real ``Embedder`` Protocol — returns a constant vector.

    The main loop wraps this via ``embed_markdown`` which returns an
    ``EmbeddingResult``; the test verifies the resulting NoteRow carries
    the vector + model + content hash.
    """

    raises: BaseException | None = None
    captured: list[tuple[str, str]] = field(default_factory=list)

    def embed(self, *, text: str, model: str) -> list[float]:
        self.captured.append((text, model))
        if self.raises is not None:
            raise self.raises
        # 768 zeros — shape-correct without being content-correlated.
        return [0.0] * 768


@dataclass
class _FakeNotesWriter:
    existing: dict[tuple[str, str], DedupHit] = field(default_factory=dict)
    written: list[Any] = field(default_factory=list)
    write_raises: BaseException | None = None

    def find_existing(self, *, source_drive_file_id: str, revision_id: str) -> DedupHit | None:
        return self.existing.get((source_drive_file_id, revision_id))

    def write(self, row) -> None:
        if self.write_raises is not None:
            raise self.write_raises
        self.written.append(row)


@dataclass
class _FakeTriagePublisher:
    """Mirrors the real publisher: returns ``None`` for non-INBOX folders
    (kind-gated skip per ADR 0037 §6) and a message id otherwise."""

    published: list[tuple[str, str]] = field(default_factory=list)
    raises: BaseException | None = None

    def publish(self, *, drive_file: DriveFile, extraction: ExtractionResult) -> str | None:
        # Mirror the real gating — INBOX-kind folders publish; reference
        # folders return None without raising.
        from agency_brain.agents.notes_ingestor.models import (
            should_publish_to_triage,
        )

        if not should_publish_to_triage(drive_file.folder):
            return None
        if self.raises is not None:
            raise self.raises
        self.published.append((drive_file.file_id, drive_file.folder.value))
        return "msg-id"


@dataclass
class _FakeWatermark:
    state: dict[str, datetime] = field(default_factory=dict)
    write_raises: BaseException | None = None

    def read(self, folder_path: str) -> datetime | None:
        return self.state.get(folder_path)

    def write(self, *, folder_path: str, last_modified_time_seen: datetime) -> None:
        if self.write_raises is not None:
            raise self.write_raises
        self.state[folder_path] = last_modified_time_seen


@dataclass
class _FakeAudit:
    emitted: list = field(default_factory=list)
    raises: BaseException | None = None

    def emit(self, event) -> None:
        if self.raises is not None:
            raise self.raises
        self.emitted.append(event)


def _file(
    *,
    file_id: str = "file-1",
    revision_id: str = "rev-1",
    name: str = "note.pdf",
    modified_time: datetime | None = None,
    folder: NoteFolder = NoteFolder.DEFAULT,
    mime_type: str = "application/pdf",
) -> DriveFile:
    return DriveFile(
        file_id=file_id,
        revision_id=revision_id,
        name=name,
        modified_time=modified_time or datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        folder=folder,
        mime_type=mime_type,
    )


def _config(
    *, folder_id: str = "folder-default", role: NoteFolder = NoteFolder.DEFAULT
) -> FolderConfig:
    return FolderConfig(folder_id=folder_id, role=role)


def _config_default(folder_id: str = "folder-default") -> FolderConfig:
    return _config(folder_id=folder_id, role=NoteFolder.DEFAULT)


def _config_hipaa(folder_id: str = "folder-hipaa") -> FolderConfig:
    return _config(folder_id=folder_id, role=NoteFolder.HIPAA)


def _run_tick(**overrides) -> Any:
    """Helper: ``run_tick`` with sensible test defaults filled in."""
    base = {
        "folders": [_config_default()],
        "drive": _FakeDrive(),
        "extractor": _FakeExtractor(),
        "embedder": _FakeEmbedder(),
        "embedding_model": "text-embedding-005",
        "notes_writer": _FakeNotesWriter(),
        "triage_publisher": _FakeTriagePublisher(),
        "watermark": _FakeWatermark(),
        "audit": _FakeAudit(),
        "sa_email": "sa@p.iam.gserviceaccount.com",
        "max_per_tick": 20,
    }
    base.update(overrides)
    return run_tick(**base)


# ---------------------------------------------------------------------------
# Tests — ADR 0031 baseline preserved
# ---------------------------------------------------------------------------


def test_fresh_file_ingested_and_published_and_moved():
    drive = _FakeDrive(files_by_folder={"folder-default": [_file()]})
    writer = _FakeNotesWriter()
    publisher = _FakeTriagePublisher()
    watermark = _FakeWatermark()
    audit = _FakeAudit()

    summary = _run_tick(
        drive=drive,
        notes_writer=writer,
        triage_publisher=publisher,
        watermark=watermark,
        audit=audit,
    )

    assert summary.listed == 1
    assert summary.written == 1
    assert summary.triage_published == 1
    assert summary.dedup_skipped == 0
    assert summary.failures == 0
    assert len(writer.written) == 1
    row = writer.written[0]
    assert row.note_id == "file-1"
    assert row.hipaa_isolated is False
    # ADR 0037 — note_kind / scope populated from folder role
    assert row.note_kind is NoteKind.INBOX
    assert row.scope is Scope.PERSONAL
    # ADR 0038 — embedding populated
    assert len(row.embedding) == 768
    assert row.embedding_model == "text-embedding-005"
    assert row.embedding_content_hash is not None
    assert row.embedding_generated_at is not None
    # Publisher + move + audit unchanged
    assert publisher.published == [("file-1", "default")]
    assert drive.moved == ["file-1"]
    assert len(audit.emitted) == 1
    assert audit.emitted[0].agent_id == "notes-ingestor"
    assert audit.emitted[0].success is True
    # Watermark advanced to the file's modifiedTime.
    assert watermark.state["folder-default"] == datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


def test_dedup_hit_skips_extract_and_write_and_publish():
    drive = _FakeDrive(files_by_folder={"folder-default": [_file()]})
    writer = _FakeNotesWriter(
        existing={
            ("file-1", "rev-1"): DedupHit(
                note_id="file-1",
                ingested_at=datetime(2026, 5, 2, 11, 0, tzinfo=UTC),
            )
        }
    )
    publisher = _FakeTriagePublisher()
    watermark = _FakeWatermark()
    audit = _FakeAudit()

    summary = _run_tick(
        drive=drive,
        notes_writer=writer,
        triage_publisher=publisher,
        watermark=watermark,
        audit=audit,
    )

    assert summary.dedup_skipped == 1
    assert summary.written == 0
    assert summary.triage_published == 0
    assert summary.failures == 0
    assert writer.written == []
    assert publisher.published == []
    assert drive.moved == ["file-1"]
    assert len(audit.emitted) == 1
    assert audit.emitted[0].success is True
    assert watermark.state["folder-default"] == datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


def test_hipaa_folder_sets_hipaa_isolated_and_publishes_with_hipaa_aspect():
    drive = _FakeDrive(
        files_by_folder={"folder-hipaa": [_file(folder=NoteFolder.HIPAA, file_id="hipaa-1")]}
    )
    writer = _FakeNotesWriter()
    publisher = _FakeTriagePublisher()

    summary = _run_tick(
        folders=[_config_hipaa()],
        drive=drive,
        notes_writer=writer,
        triage_publisher=publisher,
    )

    assert summary.written == 1
    assert writer.written[0].hipaa_isolated is True
    # ADR 0037: HIPAA folder still maps to INBOX kind + PERSONAL scope.
    assert writer.written[0].note_kind is NoteKind.INBOX
    assert writer.written[0].scope is Scope.PERSONAL
    assert publisher.published == [("hipaa-1", "hipaa")]


def test_max_per_tick_caps_total_work_across_folders():
    f1 = _file(file_id="f1", modified_time=datetime(2026, 5, 2, 10, tzinfo=UTC))
    f2 = _file(file_id="f2", modified_time=datetime(2026, 5, 2, 11, tzinfo=UTC))
    f3 = _file(
        file_id="f3",
        modified_time=datetime(2026, 5, 2, 12, tzinfo=UTC),
        folder=NoteFolder.HIPAA,
    )
    drive = _FakeDrive(
        files_by_folder={
            "folder-default": [f1, f2],
            "folder-hipaa": [f3],
        }
    )
    writer = _FakeNotesWriter()

    summary = _run_tick(
        folders=[_config_default(), _config_hipaa()],
        drive=drive,
        notes_writer=writer,
        max_per_tick=2,
    )

    assert summary.listed == 2
    assert summary.written == 2
    assert {row.note_id for row in writer.written} == {"f1", "f2"}


def test_publish_failure_still_writes_corpus_row_and_records_no_failure():
    """A publish failure leaves the BQ row + audit; the file is moved.

    The corpus is canonical — triage can be retried by republishing
    manually if needed.
    """
    drive = _FakeDrive(files_by_folder={"folder-default": [_file()]})
    publisher = _FakeTriagePublisher(raises=RuntimeError("topic missing"))
    writer = _FakeNotesWriter()

    summary = _run_tick(
        drive=drive,
        triage_publisher=publisher,
        notes_writer=writer,
    )

    assert summary.written == 1
    assert summary.triage_published == 0
    assert summary.failures == 0
    assert len(writer.written) == 1
    assert drive.moved == ["file-1"]


def test_list_failure_on_one_folder_does_not_abort_others():
    class _PartialFailDrive(_FakeDrive):
        def list_new_files(
            self,
            *,
            folder,
            since,
            mime_types: Any = None,
            name_suffixes: Any = None,
        ):
            if folder.folder_id == "folder-default":
                raise RuntimeError("permission denied")
            yield from super().list_new_files(
                folder=folder,
                since=since,
                mime_types=mime_types,
                name_suffixes=name_suffixes,
            )

    partial = _PartialFailDrive(
        files_by_folder={
            "folder-hipaa": [
                _file(file_id="hipaa-1", folder=NoteFolder.HIPAA),
            ]
        }
    )
    writer = _FakeNotesWriter()

    summary = _run_tick(
        folders=[_config_default(), _config_hipaa()],
        drive=partial,
        notes_writer=writer,
    )

    assert summary.failures == 1
    assert summary.written == 1
    assert writer.written[0].note_id == "hipaa-1"


def test_watermark_only_advances_when_progress_made():
    """No files → no watermark write."""
    drive = _FakeDrive(files_by_folder={"folder-default": []})
    watermark = _FakeWatermark(state={"folder-default": datetime(2026, 5, 1, tzinfo=UTC)})

    _run_tick(drive=drive, watermark=watermark)

    assert watermark.state["folder-default"] == datetime(2026, 5, 1, tzinfo=UTC)


def test_audit_emit_failure_is_logged_but_not_fatal():
    """An audit-emit failure must not lose the corpus row already written."""
    drive = _FakeDrive(files_by_folder={"folder-default": [_file()]})
    writer = _FakeNotesWriter()
    audit = _FakeAudit(raises=RuntimeError("audit table down"))

    summary = _run_tick(drive=drive, notes_writer=writer, audit=audit)

    assert summary.written == 1
    assert len(writer.written) == 1


# ---------------------------------------------------------------------------
# Tests — ADR 0037 / 0038 additions
# ---------------------------------------------------------------------------


def test_areas_folder_writes_corpus_row_but_does_not_publish_to_triage():
    """ADR 0037 §6: reference folders (Areas/Resources/Archives) skip
    triage publish but still land the corpus row + embedding."""
    f = _file(file_id="area-1", folder=NoteFolder.AREAS, name="ref.md", mime_type="text/markdown")
    drive = _FakeDrive(files_by_folder={"folder-areas": [f]})
    writer = _FakeNotesWriter()
    publisher = _FakeTriagePublisher()

    summary = _run_tick(
        folders=[_config(folder_id="folder-areas", role=NoteFolder.AREAS)],
        drive=drive,
        notes_writer=writer,
        triage_publisher=publisher,
    )

    assert summary.written == 1
    assert summary.triage_published == 0  # gated by note_kind
    assert publisher.published == []
    row = writer.written[0]
    assert row.note_kind is NoteKind.AREA
    assert row.scope is Scope.PERSONAL


def test_embed_failure_still_writes_corpus_row_without_embedding():
    """An embedder failure downgrades the row to no-embedding but doesn't
    drop it. Phase 5 Connector reads filter on embedding presence."""
    drive = _FakeDrive(files_by_folder={"folder-default": [_file()]})
    embedder = _FakeEmbedder(raises=RuntimeError("vertex embedding API down"))
    writer = _FakeNotesWriter()

    summary = _run_tick(drive=drive, embedder=embedder, notes_writer=writer)

    assert summary.written == 1
    assert summary.failures == 0
    row = writer.written[0]
    assert row.embedding == ()
    assert row.embedding_model is None
    # Content hash is set even when embed fails — useful for backfill
    # detection. (Actually the main loop only sets content_hash when
    # embedding succeeded; assert that current behavior.)
    assert row.embedding_content_hash is None


def test_voice_folder_passes_audio_mime_to_extractor():
    """ADR 0037 §5: a voice-memo file's MIME flows through to the
    extractor so it picks the audio prompt path."""
    voice_file = _file(
        file_id="voice-1",
        folder=NoteFolder.INBOX_VOICE,
        name="memo.m4a",
        mime_type="audio/mp4",
    )
    drive = _FakeDrive(files_by_folder={"folder-voice": [voice_file]})
    extractor = _FakeExtractor(
        default=ExtractionResult(
            markdown="## Topic\n[00:30] Some words.",
            page_count=0,
            method=ExtractionMethod.GEMINI_FLASH_AUDIO,
            confidence=0.9,
        )
    )
    writer = _FakeNotesWriter()
    publisher = _FakeTriagePublisher()

    summary = _run_tick(
        folders=[_config(folder_id="folder-voice", role=NoteFolder.INBOX_VOICE)],
        drive=drive,
        extractor=extractor,
        notes_writer=writer,
        triage_publisher=publisher,
    )

    assert summary.written == 1
    assert extractor.captured_mimes == ["audio/mp4"]
    row = writer.written[0]
    assert row.extraction_method is ExtractionMethod.GEMINI_FLASH_AUDIO
    assert row.note_kind is NoteKind.INBOX  # voice IS an inbox lane
    # Voice memos still publish to triage (they're inbox-kind).
    assert publisher.published == [("voice-1", "inbox_voice")]


# ---------------------------------------------------------------------------
# Embeddings backfill (ADR 0039 §4)
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackfillWriter:
    """Stand-in NotesWriter exposing only the backfill surface."""

    unembedded_rows: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)
    update_raises: BaseException | None = None

    def find_unembedded_rows(self, *, limit: int) -> list[dict]:
        return self.unembedded_rows[:limit]

    def update_embedding(
        self,
        *,
        note_id: str,
        revision_id: str,
        embedding,
        model: str,
        content_hash: str,
        generated_at: datetime,
    ) -> int:
        if self.update_raises is not None:
            raise self.update_raises
        self.update_calls.append(
            {
                "note_id": note_id,
                "revision_id": revision_id,
                "embedding": list(embedding),
                "model": model,
                "content_hash": content_hash,
            }
        )
        return 1


def test_backfill_embeds_and_updates_each_unembedded_row():
    from agency_brain.agents.notes_ingestor.main import (
        _run_embeddings_backfill,
    )

    writer = _FakeBackfillWriter(
        unembedded_rows=[
            {"note_id": "n1", "revision_id": "r1", "markdown_content": "first body"},
            {"note_id": "n2", "revision_id": "r2", "markdown_content": "second body"},
        ]
    )
    embedder = _FakeEmbedder()
    audit = _FakeAudit()

    result = _run_embeddings_backfill(
        notes_writer=writer,
        embedder=embedder,
        embedding_model="text-embedding-005",
        audit=audit,
        sa_email="asb-x@p.iam.gserviceaccount.com",
        max_per_tick=10,
    )

    assert result == {"scanned": 2, "embedded": 2, "updated": 2, "failures": 0}
    assert len(writer.update_calls) == 2
    assert len(audit.emitted) == 2
    # Audit output reflects backfill_embedded
    import json as _json

    payload = _json.loads(audit.emitted[0].output)
    assert payload["decision"] == "backfill_embedded"
    assert payload["embedding_dim"] == 768


def test_backfill_continues_past_per_row_failure():
    """An embed or update failure on one row mustn't abort the tick."""
    from agency_brain.agents.notes_ingestor.main import (
        _run_embeddings_backfill,
    )

    class _FlakyEmbedder(_FakeEmbedder):
        def __init__(self):
            super().__init__()
            self._n = 0

        def embed(self, *, text, model):
            self._n += 1
            if self._n == 1:
                raise RuntimeError("vertex 5xx")
            return super().embed(text=text, model=model)

    writer = _FakeBackfillWriter(
        unembedded_rows=[
            {"note_id": "n1", "revision_id": "r1", "markdown_content": "x"},
            {"note_id": "n2", "revision_id": "r2", "markdown_content": "y"},
        ]
    )
    audit = _FakeAudit()

    result = _run_embeddings_backfill(
        notes_writer=writer,
        embedder=_FlakyEmbedder(),
        embedding_model="m",
        audit=audit,
        sa_email="x",
        max_per_tick=10,
    )

    assert result["scanned"] == 2
    assert result["embedded"] == 1
    assert result["updated"] == 1
    assert result["failures"] == 1
    assert len(writer.update_calls) == 1


def test_backfill_max_per_tick_caps_scan():
    from agency_brain.agents.notes_ingestor.main import (
        _run_embeddings_backfill,
    )

    writer = _FakeBackfillWriter(
        unembedded_rows=[
            {"note_id": f"n{i}", "revision_id": "r", "markdown_content": "x"} for i in range(20)
        ]
    )
    result = _run_embeddings_backfill(
        notes_writer=writer,
        embedder=_FakeEmbedder(),
        embedding_model="m",
        audit=_FakeAudit(),
        sa_email="x",
        max_per_tick=5,
    )
    assert result["scanned"] == 5
    assert result["updated"] == 5


# ---------------------------------------------------------------------------
# ADR 0048 — Solutions Drive sweep behavior
# ---------------------------------------------------------------------------


def test_solutions_client_file_writes_area_agency_row_with_client_header() -> None:
    """End-to-end: a Solutions-client folder yields a NoteRow with
    note_kind=AREA, scope=AGENCY, and a ``Client:`` header prepended to
    markdown_content + embedded into the vector.
    """
    folder = FolderConfig(
        folder_id="meetings-folder-id",
        role=NoteFolder.SOLUTIONS_CLIENT,
        client_name="Client A",
        source_path="05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES",
        recursive=True,
    )
    drive = _FakeDrive(
        files_by_folder={
            "meetings-folder-id": [
                _file(
                    file_id="f-meeting",
                    revision_id="r-1",
                    name="2026-05-01_kickoff.pdf",
                    folder=NoteFolder.SOLUTIONS_CLIENT,
                )
            ]
        }
    )
    writer = _FakeNotesWriter()
    publisher = _FakeTriagePublisher()
    embedder = _FakeEmbedder()

    summary = _run_tick(
        folders=[folder],
        drive=drive,
        notes_writer=writer,
        triage_publisher=publisher,
        embedder=embedder,
    )

    assert summary.written == 1
    row = writer.written[0]
    assert row.note_kind is NoteKind.AREA
    assert row.scope is Scope.AGENCY
    assert row.hipaa_isolated is False
    # ADR 0048 §5 — header lines precede the body
    assert row.markdown_content.startswith(
        "Client: Client A\n"
        "Source: 05_CLIENTS/06_CLIENT_A/08_MEETING_NOTES/2026-05-01_kickoff.pdf\n\n"
    )
    # The embedder saw the header-augmented text, not the raw extraction.
    embedded_text, _ = embedder.captured[0]
    assert embedded_text.startswith("Client: Client A\n")
    # No triage publish for area-kind folders.
    assert publisher.published == []
    # No move-to-processed for Solutions reference files.
    assert drive.moved == []


def test_solutions_internal_file_writes_area_with_only_source_header() -> None:
    """Internal Solutions folders (Mgmt/Fin/Ops/Sales) have no client
    name; only the ``Source:`` line precedes the body."""
    folder = FolderConfig(
        folder_id="sales-id",
        role=NoteFolder.SOLUTIONS_INTERNAL,
        source_path="04_SALES & MARKETING (Internal)",
        recursive=True,
    )
    drive = _FakeDrive(
        files_by_folder={
            "sales-id": [
                _file(
                    file_id="f-pitch",
                    revision_id="r-1",
                    name="ecom_pitch.pdf",
                    folder=NoteFolder.SOLUTIONS_INTERNAL,
                )
            ]
        }
    )
    writer = _FakeNotesWriter()

    summary = _run_tick(folders=[folder], drive=drive, notes_writer=writer)

    assert summary.written == 1
    row = writer.written[0]
    assert row.scope is Scope.AGENCY
    assert row.note_kind is NoteKind.AREA
    # No "Client:" prefix; only "Source:"
    assert row.markdown_content.startswith(
        "Source: 04_SALES & MARKETING (Internal)/ecom_pitch.pdf\n\n"
    )
    assert "Client:" not in row.markdown_content.split("\n\n", 1)[0]


def test_maybe_prepend_source_header_noop_for_brain_folder() -> None:
    """A folder without client_name + source_path returns markdown unchanged."""
    from agency_brain.agents.notes_ingestor.main import (
        _maybe_prepend_source_header,
    )

    folder = FolderConfig(folder_id="brain-areas", role=NoteFolder.AREAS)
    out = _maybe_prepend_source_header(
        markdown="# original\nbody",
        folder=folder,
        file_name="x.md",
    )
    assert out == "# original\nbody"
