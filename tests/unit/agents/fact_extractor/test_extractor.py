"""Tests for the fact extractor parse + orchestration (ADR 0070)."""

from __future__ import annotations

from agency_brain.agents.fact_extractor.extractor import (
    ExtractorResult,
    _parse,
)
from agency_brain.agents.fact_extractor.main import run_extraction
from agency_brain.agents.fact_extractor.models import ExtractedFact, SourceNote

# ------------------------------ _parse --------------------------------------


def test_parse_empty_and_malformed_return_empty() -> None:
    assert _parse("not json") == ()
    assert _parse('{"facts": []}') == ()
    assert _parse('{"other": 1}') == ()


def test_parse_keeps_valid_normalizes_predicate_drops_incomplete() -> None:
    raw = """
    {"facts": [
      {"entity_name": "Acme", "predicate": "Retainer", "value": "$3k/mo",
       "observed_date": "2026-05-01", "confidence": 0.9, "reasoning": "stated"},
      {"entity_name": "Tim", "entity_email": "tim@acme.com",
       "predicate": "role", "value": "owner", "confidence": 0.8, "reasoning": "r"},
      {"entity_name": "", "predicate": "status", "value": "x",
       "confidence": 0.9, "reasoning": "no entity"},
      {"entity_name": "Z", "predicate": "p", "value": "",
       "confidence": 0.9, "reasoning": "no value"}
    ]}
    """
    out = _parse(raw)
    assert len(out) == 2
    assert out[0].entity_name == "Acme"
    assert out[0].predicate == "retainer"  # lowercased
    assert out[0].value == "$3k/mo"
    assert out[0].observed_date == "2026-05-01"
    assert out[1].entity_email == "tim@acme.com"
    assert out[1].predicate == "role"


def test_parse_normalizes_spaces_in_predicate() -> None:
    raw = (
        '{"facts": [{"entity_name": "X", "predicate": "Renewal Terms", '
        '"value": "annual", "confidence": 0.9, "reasoning": "r"}]}'
    )
    out = _parse(raw)
    assert out[0].predicate == "renewal_terms"


# ------------------------------ run_extraction ------------------------------


class _FakeScanner:
    def __init__(self, notes: list[SourceNote]) -> None:
        self._notes = notes
        self.calls: list[dict] = []

    def scan(self, *, since, kinds, limit) -> list[SourceNote]:
        self.calls.append({"since": since, "kinds": kinds, "limit": limit})
        return list(self._notes)


class _FakeExtractor:
    def __init__(self, per_note: dict[str, list[ExtractedFact]]) -> None:
        self._per_note = per_note

    def extract(self, *, note: SourceNote, today: str) -> ExtractorResult:
        return ExtractorResult(facts=tuple(self._per_note.get(note.note_id, [])), cost_usd=0.001)


class _FakeResolver:
    def __init__(self, by_name=None, by_email=None) -> None:
        self._by_name = by_name or {}
        self._by_email = by_email or {}
        self.asked_names: set[str] = set()
        self.asked_emails: set[str] = set()

    def resolve(self, *, names, emails):
        self.asked_names = set(names)
        self.asked_emails = set(emails)
        return (
            {k: v for k, v in self._by_name.items() if k in names},
            {k: v for k, v in self._by_email.items() if k in emails},
        )


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


def _fact(entity="Acme", email=None, predicate="retainer", value="$3k", conf=0.9, observed=None):
    return ExtractedFact(
        entity_name=entity,
        entity_email=email,
        predicate=predicate,
        value=value,
        observed_date=observed,
        confidence=conf,
        reasoning="r",
    )


def _note(note_id, ingested_at, note_date="2026-05-01"):
    return SourceNote(
        note_id=note_id,
        note_kind="email",
        markdown_content="body",
        ingested_at=ingested_at,
        note_date=note_date,
    )


def _run(scanner, extractor, resolver, writer, watermark, *, min_confidence=0.7):
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
        today="2026-06-14",
        now_iso="2026-06-14T12:00:00+00:00",
    )


def test_run_resolves_account_by_name_and_contact_by_email() -> None:
    notes = [_note("n1", "2026-06-10T00:00:00+00:00")]
    scanner = _FakeScanner(notes)
    extractor = _FakeExtractor(
        {
            "n1": [
                _fact(entity="Acme", predicate="retainer", value="$3k"),
                _fact(entity="Tim", email="Tim@Acme.com", predicate="role", value="owner"),
            ]
        }
    )
    resolver = _FakeResolver(
        by_name={"acme": ("recACME", "account")},
        by_email={"tim@acme.com": ("recTIM", "contact")},
    )
    writer = _FakeWriter()
    watermark = _FakeWatermark(cursor=None)

    stats = _run(scanner, extractor, resolver, writer, watermark)

    assert stats["facts_written"] == 2
    assert stats["resolved"] == 2
    by_pred = {r.predicate: r for r in writer.rows}
    assert by_pred["retainer"].entity_id == "recACME"
    assert by_pred["retainer"].entity_type == "account"
    assert by_pred["role"].entity_id == "recTIM"
    assert by_pred["role"].entity_type == "contact"
    # email lookups lowercased
    assert resolver.asked_emails == {"tim@acme.com"}


def test_run_unresolved_entity_keeps_name_null_id() -> None:
    notes = [_note("n1", "2026-06-10T00:00:00+00:00")]
    scanner = _FakeScanner(notes)
    extractor = _FakeExtractor({"n1": [_fact(entity="Mystery Co", value="$1k")]})
    writer = _FakeWriter()
    stats = _run(scanner, extractor, _FakeResolver(), writer, _FakeWatermark(None))
    assert stats["facts_written"] == 1
    assert stats["resolved"] == 0
    assert writer.rows[0].entity_id is None
    assert writer.rows[0].entity_name == "Mystery Co"


def test_run_observed_date_falls_back_to_note_date_then_today() -> None:
    scanner = _FakeScanner([_note("n1", "2026-06-10T00:00:00+00:00", note_date="2026-04-01")])
    extractor = _FakeExtractor(
        {
            "n1": [
                _fact(predicate="a", value="1", observed="2026-03-15"),  # explicit
                _fact(predicate="b", value="2", observed=None),  # falls back to note_date
            ]
        }
    )
    writer = _FakeWriter()
    _run(
        scanner,
        extractor,
        _FakeResolver(by_name={"acme": ("recA", "account")}),
        writer,
        _FakeWatermark(None),
    )
    by_pred = {r.predicate: r for r in writer.rows}
    assert by_pred["a"].observed_date == "2026-03-15"
    assert by_pred["b"].observed_date == "2026-04-01"


def test_run_drops_below_confidence_floor_and_advances_watermark() -> None:
    scanner = _FakeScanner([_note("n1", "2026-06-10T00:00:00+00:00")])
    extractor = _FakeExtractor(
        {"n1": [_fact(predicate="keep", conf=0.75), _fact(predicate="drop", conf=0.5)]}
    )
    writer = _FakeWriter()
    watermark = _FakeWatermark(None)
    stats = _run(scanner, extractor, _FakeResolver(), writer, watermark, min_confidence=0.7)
    assert [r.predicate for r in writer.rows] == ["keep"]
    assert watermark.written == ["2026-06-10T00:00:00+00:00"]
    assert stats["facts_written"] == 1


def test_run_no_notes_noop() -> None:
    writer = _FakeWriter()
    wm = _FakeWatermark("2026-06-09T00:00:00+00:00")
    stats = _run(_FakeScanner([]), _FakeExtractor({}), _FakeResolver(), writer, wm)
    assert stats["facts_written"] == 0
    assert writer.rows == []
    assert wm.written == []
