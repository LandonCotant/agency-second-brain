"""Tests for ``GalaxyIndexer`` (ADR 0054 §2 — drop-to-index Galaxy sweep)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from agency_brain.agents.librarian.galaxy_indexer import (
    GALAXY_PARENT_FOLDER_ROLE,
    GalaxyIndexer,
    GalaxySweepSummary,
)
from agency_brain.agents.librarian.ingestor import (
    IngestOutcome,
    LibrarianIngestor,
)
from agency_brain.agents.librarian.models import (
    LibrarianOutcome,
    LinkerOutcome,
)
from agency_brain.agents.notes_ingestor.models import NoteKind, Scope

# --------------------------------------------------------------- fakes


class _FakeDriveClient:
    """List + download + export against a canned in-memory tree."""

    def __init__(
        self,
        *,
        tree: dict[str, list[dict]] | None = None,
        blobs: dict[str, bytes] | None = None,
        exports: dict[str, bytes] | None = None,
    ) -> None:
        self._tree = tree or {}
        self._blobs = blobs or {}
        self._exports = exports or {}
        self.list_calls: list[str] = []
        self.download_calls: list[str] = []
        self.export_calls: list[str] = []

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]:
        self.list_calls.append(folder_id)
        return list(self._tree.get(folder_id, []))

    def download_file(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return self._blobs.get(file_id, b"")

    def export_doc_as_markdown(self, file_id: str) -> bytes:
        self.export_calls.append(file_id)
        return self._exports.get(file_id, b"")


@dataclass
class _ExtractResult:
    markdown: str


class _FakeExtractor:
    """Stand-in for GeminiMultimodalExtractor.

    Mirrors production behavior for Google Docs (already-exported
    markdown bytes are passed through) so the Galaxy indexer test
    assertions match what the real extractor would emit.
    """

    GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"

    def __init__(self, *, markdown: str = "EXTRACTED") -> None:
        self._markdown = markdown
        self.calls: list[tuple[bytes, str, str]] = []

    def extract(self, *, data: bytes, mime_type: str, file_name: str) -> _ExtractResult:
        self.calls.append((data, mime_type, file_name))
        if mime_type == self.GOOGLE_DOC_MIME_TYPE:
            # Production extractor passthroughs already-exported markdown.
            return _ExtractResult(markdown=data.decode("utf-8", errors="replace"))
        return _ExtractResult(markdown=self._markdown)


class _FakeIngestor:
    """Mimics LibrarianIngestor.ingest() — captures call args for assertions."""

    def __init__(self, *, outcome: IngestOutcome | None = None) -> None:
        self._outcome = outcome or IngestOutcome(note_id="note-1", written=True, embedded=True)
        self.calls: list[dict] = []

    def ingest(self, **kwargs) -> IngestOutcome:
        self.calls.append(kwargs)
        return self._outcome


class _FakeLinker:
    def __init__(self, *, outcome: LinkerOutcome | None = None) -> None:
        self._outcome = outcome or LinkerOutcome(neighbors_linked=2)
        self.calls: list[dict] = []

    def link_for_drive_file(self, **kwargs) -> LinkerOutcome:
        self.calls.append(kwargs)
        return self._outcome


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.outcomes: list[LibrarianOutcome] = []

    def emit_file_outcome(self, *, run_id, outcome: LibrarianOutcome, latency_ms: int) -> None:
        self.outcomes.append(outcome)


# --------------------------------------------------------------- fixtures


def _folder(file_id: str, name: str) -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-05-15T12:00:00Z",
    }


def _md_file(file_id: str, name: str = "atomic-note.md") -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": "text/markdown",
        "modifiedTime": "2026-05-15T12:00:00Z",
        "webViewLink": f"https://drive/{file_id}",
    }


def _make_indexer(
    *,
    drive: _FakeDriveClient,
    ingestor: _FakeIngestor | None = None,
    linker: _FakeLinker | None = None,
    audit: _FakeAuditWriter | None = None,
    extractor: _FakeExtractor | None = None,
) -> tuple[GalaxyIndexer, _FakeIngestor, _FakeLinker, _FakeAuditWriter]:
    ing = ingestor or _FakeIngestor()
    lk = linker or _FakeLinker()
    aw = audit or _FakeAuditWriter()
    ext = extractor or _FakeExtractor()
    idx = GalaxyIndexer(
        drive_client=drive,
        extractor=ext,
        ingestor=ing,
        linker=lk,
        audit_writer=aw,
    )
    return idx, ing, lk, aw


# --------------------------------------------------------------- tests


def test_sweep_indexes_each_md_file_with_galaxy_kind() -> None:
    drive = _FakeDriveClient(
        tree={
            "galaxy-root": [_md_file("f1"), _md_file("f2", "second.md")],
        },
        blobs={
            "f1": b"# Atomic note one\n\nbody.",
            "f2": b"# Second\n\nbody.",
        },
    )
    idx, ing, lk, aw = _make_indexer(drive=drive)
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")

    assert summary.listed == 2
    assert summary.indexed == 2
    assert summary.deduped == 0
    assert summary.failed == 0
    # Each file → one ingest call with kind_override=GALAXY + scope=PERSONAL.
    assert len(ing.calls) == 2
    for c in ing.calls:
        assert c["kind_override"] is NoteKind.GALAXY
        assert c["scope_override"] is Scope.PERSONAL
        assert c["dest_folder"] is None
        # Markdown decoded inline (no LLM call for text/markdown).
        assert "Atomic" in c["markdown"] or "Second" in c["markdown"]
    # Linker invoked once per fresh write.
    assert len(lk.calls) == 2
    # Audit emits per-file outcome with galaxy parent_folder_role.
    assert len(aw.outcomes) == 2
    for o in aw.outcomes:
        assert o.from_folder_role == GALAXY_PARENT_FOLDER_ROLE
        assert o.moved is False  # ADR 0054 §2 — drop-to-index, no move


def test_sweep_recurses_into_subfolders() -> None:
    drive = _FakeDriveClient(
        tree={
            "galaxy-root": [
                _folder("sub-id", "concepts"),
                _md_file("f-top", "top-level.md"),
            ],
            "sub-id": [_md_file("f-nested", "deep.md")],
        },
        blobs={"f-top": b"top.", "f-nested": b"nested."},
    )
    idx, ing, _, aw = _make_indexer(drive=drive)
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    indexed_names = {c["drop_file"].name for c in ing.calls}
    assert indexed_names == {"top-level.md", "deep.md"}
    assert summary.listed == 2
    # Audit emitted for both files including the nested one.
    nested_audit = [o for o in aw.outcomes if o.file_name == "deep.md"]
    assert len(nested_audit) == 1
    # The nested file's to_folder_path captures the walked path.
    assert "concepts" in (nested_audit[0].to_folder_path or "")


def test_sweep_skips_when_galaxy_folder_id_empty() -> None:
    drive = _FakeDriveClient()
    idx, ing, lk, aw = _make_indexer(drive=drive)
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="")
    assert summary == GalaxySweepSummary()
    assert ing.calls == []
    assert lk.calls == []
    assert aw.outcomes == []


def test_sweep_dedup_hit_does_not_call_linker() -> None:
    """On dedup hit, the existing row already has its neighbors — skip
    the linker BQ query."""
    drive = _FakeDriveClient(
        tree={"galaxy-root": [_md_file("f1")]},
        blobs={"f1": b"content"},
    )
    dedup_outcome = IngestOutcome(note_id="existing-1", written=False, deduped=True, embedded=False)
    ing = _FakeIngestor(outcome=dedup_outcome)
    idx, _, lk, aw = _make_indexer(drive=drive, ingestor=ing)
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    assert summary.indexed == 0
    assert summary.deduped == 1
    assert len(lk.calls) == 0
    assert len(aw.outcomes) == 1


def test_sweep_continues_when_one_file_errors() -> None:
    drive = _FakeDriveClient(
        tree={"galaxy-root": [_md_file("f-ok"), _md_file("f-bad", "bad.md")]},
        blobs={"f-ok": b"ok.", "f-bad": b"bad."},
    )

    class _PartialIngestor(_FakeIngestor):
        def __init__(self) -> None:
            super().__init__()
            self._count = 0

        def ingest(self, **kwargs):
            self._count += 1
            self.calls.append(kwargs)
            if self._count == 2:
                raise RuntimeError("ingest boom")
            return self._outcome

    ing = _PartialIngestor()
    idx, _, _, aw = _make_indexer(drive=drive, ingestor=ing)
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    assert summary.listed == 2
    assert summary.indexed == 1
    assert summary.failed == 1
    # Audit row emitted for the failure too.
    error_rows = [o for o in aw.outcomes if o.error is not None]
    assert len(error_rows) == 1
    assert "ingest boom" in error_rows[0].error


def test_sweep_uses_extractor_for_non_text_mimes() -> None:
    """PDF / docx / etc. mime types route through the multimodal extractor."""
    drive = _FakeDriveClient(
        tree={
            "galaxy-root": [
                {
                    "id": "f-pdf",
                    "name": "diagram.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-05-15T12:00:00Z",
                    "webViewLink": "https://drive/f-pdf",
                }
            ]
        },
        blobs={"f-pdf": b"%PDF-..."},
    )
    extractor = _FakeExtractor(markdown="EXTRACTED-FROM-PDF")
    idx, ing, _, _ = _make_indexer(drive=drive, extractor=extractor)
    idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    assert len(extractor.calls) == 1
    assert extractor.calls[0][1] == "application/pdf"
    # Extracted markdown flows into the ingestor.
    assert ing.calls[0]["markdown"] == "EXTRACTED-FROM-PDF"


def test_sweep_exports_google_docs_as_markdown() -> None:
    drive = _FakeDriveClient(
        tree={
            "galaxy-root": [
                {
                    "id": "f-gdoc",
                    "name": "Big Idea",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-05-15T12:00:00Z",
                    "webViewLink": "https://drive/f-gdoc",
                }
            ]
        },
        exports={"f-gdoc": b"# Big Idea\n\nBody."},
    )
    idx, ing, _, _ = _make_indexer(drive=drive)
    idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    assert "f-gdoc" in drive.export_calls
    assert "Big Idea" in ing.calls[0]["markdown"]


def test_sweep_respects_max_depth() -> None:
    """A folder nested deeper than max_depth=2 is not walked."""
    drive = _FakeDriveClient(
        tree={
            "galaxy-root": [_folder("L1", "l1")],
            "L1": [_folder("L2", "l2")],
            "L2": [_md_file("deep", "too-deep.md")],
        },
        blobs={"deep": b"deep."},
    )
    idx = GalaxyIndexer(
        drive_client=drive,
        extractor=_FakeExtractor(),
        ingestor=_FakeIngestor(),
        linker=_FakeLinker(),
        audit_writer=_FakeAuditWriter(),
        max_depth=2,
    )
    summary = idx.sweep(run_id="run-1", galaxy_folder_id="galaxy-root")
    # Walk only descended one level (root + L1); files inside L2 not listed.
    assert summary.listed == 0


def test_real_ingestor_with_kind_override_writes_galaxy_kind() -> None:
    """Smoke: end-to-end with the *real* LibrarianIngestor (not a fake)
    confirms kind_override flows from sweep → ingestor → note_kind on
    the row."""
    import json as _json

    from agency_brain.agents.librarian.models import DropFile
    from agency_brain.agents.notes_ingestor.embedder import Embedder
    from agency_brain.agents.notes_ingestor.writer import NotesWriter

    class _BQ:
        def __init__(self):
            self.table = {}
            self.queries = []

        def insert_rows_json(self, table_ref, rows):  # pragma: no cover
            raise AssertionError("ADR 0070 — Librarian writes via MERGE, not insert")

        def query_rows(self, sql, parameters=None):
            self.queries.append((sql, parameters))
            params = {p["name"]: p["value"] for p in (parameters or [])}
            if "MERGE" in sql:
                for r in _json.loads(params["rows_json"]):
                    self.table[r["note_id"]] = r
            return []

    class _Embedder(Embedder):
        def embed(self, *, text, model):
            return [0.1] * 768

    class _DriveMeta:
        def get_file_meta(self, file_id):
            return {
                "id": file_id,
                "headRevisionId": "rev-1",
                "modifiedTime": "2026-05-15T12:00:00Z",
                "webViewLink": f"https://drive/{file_id}",
            }

    bq = _BQ()
    real_ingestor = LibrarianIngestor(
        notes_writer=NotesWriter(bq_rows=bq, bq_query=bq, project_id="p"),
        embedder=_Embedder(),
        drive_meta=_DriveMeta(),
    )
    drop = DropFile(
        file_id="gal-1",
        name="atom.md",
        mime_type="text/markdown",
        parent_folder_id="galaxy-root",
        parent_folder_role=GALAXY_PARENT_FOLDER_ROLE,
        modified_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        web_view_link="https://drive/gal-1",
    )
    out = real_ingestor.ingest(
        drop_file=drop,
        dest_folder=None,
        markdown="# atom",
        kind_override=NoteKind.GALAXY,
        scope_override=Scope.PERSONAL,
    )
    assert out.written
    row = bq.table["gal-1"]
    assert row["note_kind"] == "galaxy"
    assert row["scope"] == "personal"
    assert row["extraction_notes"] == "librarian: galaxy_index"
