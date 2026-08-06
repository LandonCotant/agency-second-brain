"""End-to-end-ish unit test for the Brag Spotter agent (ADR 0043)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from agency_brain.agents.brag_spotter.agent import BragSpotterAgent
from agency_brain.agents.brag_spotter.composer import BragSpotterComposer
from agency_brain.agents.brag_spotter.digest import DigestDispatcher
from agency_brain.agents.brag_spotter.models import (
    BragSpotterInput,
)
from agency_brain.agents.brag_spotter.readers import (
    ExistingWinsForWeekReader,
    RecentDecisionsReader,
    RecentNotesReader,
    RecentReflectionsReader,
    RecentRoutedEventsReader,
    RecentTriagedItemsReader,
)
from agency_brain.agents.brag_spotter.writer import WinsWriter
from agency_brain.common.memory_bank import InMemoryMemoryBank

# ---------------------------------------------------------------- fakes


@dataclass
class _FakeBQReadAdapter:
    rows_by_table: dict[str, list[dict]] = field(default_factory=dict)
    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        for token, rows in self.rows_by_table.items():
            if token in sql:
                return list(rows)
        return []


@dataclass
class _FakeBQWriteAdapter:
    inserted: list[tuple[str, list[dict]]] = field(default_factory=list)
    errors_to_return: list = field(default_factory=list)

    def insert_rows_json(self, table_ref: str, rows: list[dict]) -> list:
        self.inserted.append((table_ref, rows))
        return list(self.errors_to_return)


@dataclass
class _FakeBQQueryParamAdapter:
    rows_to_return: list[dict] = field(default_factory=list)

    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        return list(self.rows_to_return)


@dataclass
class _FakeLLM:
    canned: str

    def generate(self, *, prompt: str, model: str) -> str:
        return self.canned


@dataclass
class _FakeChatResult:
    ok: bool = True
    status: int = 200


@dataclass
class _FakeChatClient:
    last_text: str | None = None

    def send(self, text: str) -> _FakeChatResult:
        self.last_text = text
        return _FakeChatResult()


@dataclass
class _FakeGmailClient:
    drafted: tuple[str, str, str] | None = None

    def draft(self, *, recipient_email: str, subject: str, body_markdown: str) -> str:
        self.drafted = (recipient_email, subject, body_markdown)
        return "r-99"


@dataclass
class _FakeAudit:
    rows: list[dict] = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.rows.append(event)


# ---------------------------------------------------------------- harness


def _build_agent(
    *,
    llm_response: str,
    bq_rows: dict[str, list[dict]],
    existing_wins_dedup: bool = False,
):
    bq_read = _FakeBQReadAdapter(rows_by_table=bq_rows)
    bq_write = _FakeBQWriteAdapter()
    bq_query = _FakeBQQueryParamAdapter(rows_to_return=[{"1": 1}] if existing_wins_dedup else [])

    composer = BragSpotterComposer(
        prompt_template="W:{{week_of_human}} R:{{recipient_email}} "
        "T:{{triaged_items_block}} E:{{existing_wins_block}} "
        "RT:{{routed_events_block}} N:{{notes_block}} "
        "D:{{decisions_block}} F:{{reflections_block}}",
        llm=_FakeLLM(canned=llm_response),
    )

    triaged = RecentTriagedItemsReader(bq_client=bq_read, project_id="p")
    routed = RecentRoutedEventsReader(bq_client=bq_read, project_id="p")
    notes = RecentNotesReader(bq_client=bq_read, project_id="p")
    decisions = RecentDecisionsReader(bq_client=bq_read, project_id="p")
    reflections = RecentReflectionsReader(bq_client=bq_read, project_id="p")
    existing = ExistingWinsForWeekReader(bq_client=bq_read, project_id="p")
    writer = WinsWriter(bq=bq_write, bq_query=bq_query, project_id="p")
    dispatcher = DigestDispatcher(chat=_FakeChatClient(), gmail=_FakeGmailClient())

    agent = BragSpotterAgent(
        sa_email="asb-brag-spotter-sa@x.iam",
        audit_log=_FakeAudit(),
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        triaged_items_reader=triaged,
        routed_events_reader=routed,
        notes_reader=notes,
        decisions_reader=decisions,
        reflections_reader=reflections,
        existing_wins_reader=existing,
        wins_writer=writer,
        dispatcher=dispatcher,
    )
    return agent, bq_write


# ---------------------------------------------------------------- tests


def test_agent_quiet_week_no_sources_no_candidates():
    """Quiet week — empty sources + LLM returns commentary-only.

    Agent should still complete successfully, send digest, write 0 wins.
    """
    llm = json.dumps({"commentary": "Quiet week.", "candidates": []})
    agent, bq_write = _build_agent(llm_response=llm, bq_rows={})

    out = agent.invoke(
        BragSpotterInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 4),
            week_of=date(2026, 5, 4),
        )
    )
    assert out.wins_written == 0
    assert out.wins_skipped == 0
    assert out.candidates == ()
    assert out.body_markdown == "Quiet week."
    assert out.gmail_draft_id == "r-99"
    assert out.chat_status == 200
    assert out.sources_seen == ()
    assert bq_write.inserted == []


def test_agent_active_week_writes_candidates():
    """Active week — readers return data, LLM emits 2 candidates."""
    llm = json.dumps(
        {
            "commentary": "Two wins this week.",
            "candidates": [
                {
                    "title": "Closed Acme",
                    "source_kind": "decision",
                    "source_id": "d-1",
                    "summary": "5-day loop",
                },
                {
                    "title": "Caught Client A churn early",
                    "source_kind": "triaged_item",
                    "source_id": "i-1",
                },
            ],
        }
    )
    agent, bq_write = _build_agent(
        llm_response=llm,
        bq_rows={
            "agent_outputs.triaged_items": [
                {
                    "item_id": "i-1",
                    "triaged_at": datetime(2026, 5, 1, tzinfo=UTC),
                    "severity": "high",
                    "reasoning": "churn signal",
                    "source": "gmail",
                    "source_url": None,
                    "positive_goal_achieving": None,
                }
            ],
            "agent_outputs.decisions": [
                {
                    "decision_id": "d-1",
                    "decided_at": datetime(2026, 5, 2, tzinfo=UTC),
                    "title": "Pursue Acme",
                    "context": "y",
                    "choice": "yes",
                    "status": "pending",
                }
            ],
        },
    )
    out = agent.invoke(
        BragSpotterInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 4),
            week_of=date(2026, 5, 4),
        )
    )
    assert out.wins_written == 2
    assert out.wins_skipped == 0
    assert "triaged_items" in out.sources_seen
    assert "decisions" in out.sources_seen
    assert len(bq_write.inserted) == 2  # one INSERT per candidate


def test_agent_dedup_hit_skips_existing_candidates():
    """All candidates already exist → wins_skipped==N, wins_written==0."""
    llm = json.dumps(
        {
            "commentary": "x",
            "candidates": [
                {
                    "title": "Closed Acme",
                    "source_kind": "decision",
                    "source_id": "d-1",
                }
            ],
        }
    )
    agent, bq_write = _build_agent(llm_response=llm, bq_rows={}, existing_wins_dedup=True)
    out = agent.invoke(
        BragSpotterInput(
            recipient_email="x@y.com",
            run_date=date(2026, 5, 4),
            week_of=date(2026, 5, 4),
        )
    )
    assert out.wins_written == 0
    assert out.wins_skipped == 1
    assert bq_write.inserted == []  # dedup hit suppresses INSERT


def test_agent_parse_error_falls_back_to_quiet_week():
    """LLM returns garbage → quiet-week fallback fires (ADR 0043 §7)."""
    llm = "not json"
    agent, bq_write = _build_agent(llm_response=llm, bq_rows={})
    out = agent.invoke(
        BragSpotterInput(
            recipient_email="x@y.com",
            run_date=date(2026, 5, 4),
            week_of=date(2026, 5, 4),
        )
    )
    # Fell back to quiet-week digest — still sent Gmail/Chat, wrote 0 wins.
    assert out.wins_written == 0
    assert out.candidates == ()
    assert "May 04, 2026" in out.body_markdown
    assert out.gmail_draft_id == "r-99"
