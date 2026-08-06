"""Tests for the commitment extractor parse + orchestration (ADR 0069)."""

from __future__ import annotations

from agency_brain.agents.commitment_extractor.extractor import (
    Extractor,
    ExtractorConfig,
    ExtractorResult,
    _parse,
)
from agency_brain.agents.commitment_extractor.main import run_extraction
from agency_brain.agents.commitment_extractor.models import (
    ExtractedCommitment,
    SourceNote,
)

# ------------------------------ _parse --------------------------------------


def test_parse_empty_and_malformed_return_empty() -> None:
    assert _parse("not json") == ()
    assert _parse('{"commitments": []}') == ()
    assert _parse('{"other": 1}') == ()


def test_parse_keeps_valid_drops_bad_direction_and_empty_text() -> None:
    raw = """
    {"commitments": [
      {"direction": "mine", "commitment_text": "send proposal",
       "counterparty_email": "x@y.com", "due_date": "2026-06-20",
       "confidence": 0.9, "reasoning": "explicit promise"},
      {"direction": "sideways", "commitment_text": "bad direction",
       "confidence": 0.9, "reasoning": "r"},
      {"direction": "theirs", "commitment_text": "",
       "confidence": 0.9, "reasoning": "empty text"}
    ]}
    """
    out = _parse(raw)
    assert len(out) == 1
    c = out[0]
    assert c.direction == "mine"
    assert c.commitment_text == "send proposal"
    assert c.counterparty_email == "x@y.com"
    assert c.due_date == "2026-06-20"
    assert c.confidence == 0.9


# ------------------------------ run_extraction ------------------------------


class _FakeScanner:
    def __init__(self, notes: list[SourceNote]) -> None:
        self._notes = notes
        self.calls: list[dict] = []

    def scan(self, *, since, kinds, limit) -> list[SourceNote]:
        self.calls.append({"since": since, "kinds": kinds, "limit": limit})
        return list(self._notes)


class _FakeExtractor:
    def __init__(self, per_note: dict[str, list[ExtractedCommitment]]) -> None:
        self._per_note = per_note
        self.seen: list[str] = []

    def extract(self, *, note: SourceNote, today: str) -> ExtractorResult:
        self.seen.append(note.note_id)
        return ExtractorResult(
            commitments=tuple(self._per_note.get(note.note_id, [])),
            cost_usd=0.001,
        )


class _FakeResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.asked: set[str] = set()

    def resolve(self, emails: set[str]) -> dict[str, str]:
        self.asked = set(emails)
        return {e: self._mapping[e] for e in emails if e in self._mapping}


class _FakeWriter:
    def __init__(self) -> None:
        self.rows: list = []

    def insert(self, rows) -> None:
        self.rows.extend(rows)


class _FakeWatermark:
    def __init__(self, cursor: str | None) -> None:
        self.cursor = cursor
        self.written: list[str] = []

    def read(self) -> str | None:
        return self.cursor

    def write(self, cursor: str) -> None:
        self.written.append(cursor)


def _commit(direction="mine", text="do x", email=None, conf=0.9, due=None):
    return ExtractedCommitment(
        direction=direction,
        commitment_text=text,
        counterparty_email=email,
        counterparty_name=None,
        due_date=due,
        confidence=conf,
        reasoning="r",
    )


def _note(note_id: str, ingested_at: str) -> SourceNote:
    return SourceNote(
        note_id=note_id,
        note_kind="email",
        markdown_content="body",
        ingested_at=ingested_at,
    )


def _run(scanner, extractor, resolver, writer, watermark, *, min_confidence=0.6):
    return run_extraction(
        extractor=extractor,
        scanner=scanner,
        resolver=resolver,
        writer=writer,
        watermark=watermark,
        source_kinds=["email", "inbox"],
        min_confidence=min_confidence,
        max_notes=200,
        run_id="run1",
        today="2026-06-13",
        now_iso="2026-06-13T12:00:00+00:00",
    )


def test_run_extraction_writes_resolves_and_advances_watermark() -> None:
    notes = [
        _note("n1", "2026-06-10T00:00:00+00:00"),
        _note("n2", "2026-06-12T00:00:00+00:00"),
    ]
    scanner = _FakeScanner(notes)
    extractor = _FakeExtractor(
        {
            "n1": [_commit(text="send deck", email="Tim@Acme.com", conf=0.9)],
            "n2": [_commit(direction="theirs", text="send docs", conf=0.8)],
        }
    )
    resolver = _FakeResolver({"tim@acme.com": "recACME"})
    writer = _FakeWriter()
    watermark = _FakeWatermark(cursor="2026-06-09T00:00:00+00:00")

    stats = _run(scanner, extractor, resolver, writer, watermark)

    assert stats["scanned"] == 2
    assert stats["commitments_written"] == 2
    assert stats["watermark_advanced"] is True
    # Watermark advanced to the max ingested_at seen.
    assert watermark.written == ["2026-06-12T00:00:00+00:00"]
    # Account resolution is case-insensitive (email lowercased on lookup).
    assert resolver.asked == {"tim@acme.com"}
    by_text = {r.commitment_text: r for r in writer.rows}
    assert by_text["send deck"].account_id == "recACME"
    assert by_text["send deck"].direction == "mine"
    assert by_text["send docs"].account_id is None
    assert by_text["send docs"].direction == "theirs"
    # Provenance + status defaults.
    assert all(r.status == "open" for r in writer.rows)
    assert by_text["send deck"].source_note_id == "n1"
    assert all(r.agent_run_id == "run1" for r in writer.rows)


def test_run_extraction_drops_below_confidence_floor() -> None:
    notes = [_note("n1", "2026-06-10T00:00:00+00:00")]
    scanner = _FakeScanner(notes)
    extractor = _FakeExtractor(
        {
            "n1": [
                _commit(text="keep", conf=0.7),
                _commit(text="drop", conf=0.4),
            ]
        }
    )
    writer = _FakeWriter()
    watermark = _FakeWatermark(cursor=None)

    stats = _run(scanner, extractor, _FakeResolver({}), writer, watermark, min_confidence=0.6)

    assert stats["commitments_written"] == 1
    assert [r.commitment_text for r in writer.rows] == ["keep"]
    # Watermark still advances past the scanned note even though one commit dropped.
    assert watermark.written == ["2026-06-10T00:00:00+00:00"]


def test_run_extraction_no_notes_does_not_write_or_advance() -> None:
    scanner = _FakeScanner([])
    writer = _FakeWriter()
    watermark = _FakeWatermark(cursor="2026-06-09T00:00:00+00:00")

    stats = _run(scanner, _FakeExtractor({}), _FakeResolver({}), writer, watermark)

    assert stats == {
        "scanned": 0,
        "commitments_written": 0,
        "cost_usd": 0.0,
        "watermark_advanced": False,
    }
    assert writer.rows == []
    assert watermark.written == []


def test_run_extraction_passes_cursor_and_kinds_to_scanner() -> None:
    scanner = _FakeScanner([])
    _run(
        scanner,
        _FakeExtractor({}),
        _FakeResolver({}),
        _FakeWriter(),
        _FakeWatermark(cursor="2026-06-01T00:00:00+00:00"),
    )
    assert scanner.calls[0]["since"] == "2026-06-01T00:00:00+00:00"
    assert scanner.calls[0]["kinds"] == ["email", "inbox"]


def test_extractor_config_defaults() -> None:
    cfg = ExtractorConfig(project_id="p")
    assert cfg.model == "gemini-2.5-flash"
    # Smoke: Extractor constructs without a client (lazy-built on first call).
    Extractor(cfg)
