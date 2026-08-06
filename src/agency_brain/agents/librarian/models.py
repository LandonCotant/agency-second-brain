"""Librarian dataclasses (ADR 0044 + the daily-reflection-doc plan, Phase D).

Inputs are Drive files in ``Brain/Inbox/Drop/`` (and optional age-based
sweep of ``Brain/Inbox/QuickNotes/``). Outputs are: (a) the file moved
into ``Brain/Areas/<topic>/``, (b) bidirectional ``notes_links`` rows
for the file's top-K semantic neighbors, (c) an auto-edited ``## Related``
section in the destination dossier doc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DropFile:
    """One Drive file pulled from a watched Inbox folder.

    Mirrors ``notes_ingestor.models.DriveFileSnippet`` ergonomically but
    stays a separate class so future Librarian-specific fields (e.g.
    classification confidence carried back to the audit row) don't leak
    into the corpus ingestor.
    """

    file_id: str
    name: str
    mime_type: str
    parent_folder_id: str
    """The Inbox/Drop folder id this file currently lives in."""
    parent_folder_role: str
    """``drop`` | ``quicknotes``. Drives the sort-vs-publish decision."""
    modified_time: datetime
    web_view_link: str | None = None


@dataclass(frozen=True)
class AreaFolder:
    """One candidate destination folder for the Librarian classifier.

    ``id`` is the Drive folder id (used by the mover for the actual
    move). ``path`` is the candidate string the classifier sees + matches
    against. ``root_id`` / ``root_label`` track which root this folder
    descends from when destinations span multiple Drives (e.g. a Brain
    Areas root + a the agency clients root).
    """

    id: str
    name: str
    """Folder display name, e.g. ``clienta-pi``."""
    path: str
    """Path the classifier sees. Multi-root configs prefix with the
    root's label so the LLM can disambiguate (e.g. ``brain/personal``
    vs ``clients/clienta-pi/01_STRATEGY``)."""
    root_id: str = ""
    """Drive folder id of the root this folder descends from. Empty on
    legacy single-root constructions."""
    root_label: str = ""
    """Human-readable label for the root, e.g. ``brain`` or ``clients``."""
    bucket: str = "areas"
    """IPARAG bucket this root maps to (ADR 0054). ``areas`` (default;
    also covers ``clients``) → ``note_kind=area``; ``resources`` →
    ``note_kind=resource``. Drives kind/scope downstream without an LLM
    schema change."""


@dataclass(frozen=True)
class LibrarianClassification:
    """LLM classifier output for one Drop file.

    ``dest_folder_path`` is the Areas-relative path the file should land
    under. ``None`` → no confident match → file moves to
    ``Brain/Areas/_uncategorized/`` instead. ``confidence`` is the LLM's
    self-rating in [0, 1]; values below the threshold also fall back
    to ``_uncategorized/``.

    ``suggested_description`` is a short (≤6 word) description used by
    the renamer when the original filename is anonymous (e.g.
    ``Notes_260502_*.pdf``). Falls back to a generic if absent.
    """

    dest_folder_path: str | None
    confidence: float
    reasoning: str
    suggested_description: str | None = None


@dataclass(frozen=True)
class LinkerOutcome:
    """Per-file linker bookkeeping for the audit row.

    Captures both the BQ-side notes_links populate AND the Drive-side
    dossier section update so a single audit row tells the full story.
    """

    neighbors_linked: int = 0
    """Number of bidirectional ``notes_links`` rows inserted (one per
    neighbor in the new file's set; doubled when bidirectional inserts
    both directions)."""
    related_section_updated: bool = False
    """True iff the destination dossier's ``## Related`` block was
    re-rendered via ``DossierSectionEditor``."""
    dossier_doc_id: str | None = None
    """Drive id of the dossier doc edited (if any)."""


@dataclass(frozen=True)
class IngestOutcomeSummary:
    """Phase G — Librarian-as-ingestor bookkeeping for the audit row.

    Stays separate from ``LinkerOutcome`` because the ingest step (write
    to ``agent_outputs.notes``) and the link step (write to
    ``notes_links``) are independent — either can succeed while the
    other fails.
    """

    note_id: str | None = None
    written: bool = False
    deduped: bool = False
    embedded: bool = False
    error: str | None = None


@dataclass(frozen=True)
class LibrarianOutcome:
    """One file's full lifecycle through the Librarian tick.

    ``moved`` is False when classification fell back to
    ``_uncategorized/`` AND the move was suppressed (e.g. the file was
    already there). ``error`` carries the type+message when the per-file
    try/except trapped a failure mid-stream — the lister still continues
    the next file.
    """

    file_id: str
    file_name: str
    from_folder_role: str
    to_folder_path: str | None
    confidence: float
    moved: bool
    linker: LinkerOutcome = field(default_factory=LinkerOutcome)
    ingest: IngestOutcomeSummary = field(default_factory=IngestOutcomeSummary)
    error: str | None = None


@dataclass(frozen=True)
class LibrarianSummary:
    """Aggregate across one Cloud Run Job execution. Used by ``main.py``
    to log a single ``librarian.done`` line and emit a summary audit row.

    Galaxy counters (ADR 0054 §2) are 0 when ``BRAIN_GALAXY_FOLDER_ID``
    is unset or empty — the sweep skips silently.
    """

    listed: int
    moved: int
    uncategorized: int
    failed: int
    neighbors_linked_total: int
    related_sections_updated: int
    galaxy_listed: int = 0
    galaxy_indexed: int = 0
    galaxy_deduped: int = 0
    galaxy_failed: int = 0
