"""Unit tests for MorningBriefAgent.

The agent orchestrates readers + calendar + composer + Gmail drafts +
writer dedup. Each dependency is replaced with a tiny fake so the
test stays fast and deterministic. The audit log + memory bank come
from the production utilities (in-memory variants).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agency_brain.agents.morning_brief.agent import MorningBriefAgent
from agency_brain.agents.morning_brief.calendar_client import CalendarClient
from agency_brain.agents.morning_brief.composer import MorningBriefComposer
from agency_brain.agents.morning_brief.gmail_drafts_client import (
    GmailDraftsClient,
)
from agency_brain.agents.morning_brief.models import MorningBriefInput
from agency_brain.agents.morning_brief.readers import (
    DraftsAwaitingReviewReader,
    OpenTasksForOwnerReader,
    RiskFlagsReader,
    TriagedItemsForOwnerReader,
)
from agency_brain.agents.morning_brief.writer import MorningBriefWriter
from agency_brain.common.audit_log import AuditLogClient
from agency_brain.common.memory_bank import InMemoryMemoryBank


@dataclass
class _FakeBQ:
    rows: list[dict] = field(default_factory=list)
    last_sql: str = ""

    def query_rows(self, sql: str) -> list[dict]:
        self.last_sql = sql
        return list(self.rows)


@dataclass
class _FakeBQRows:
    captured: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.captured.append((table_ref, rows))
        return []


@dataclass
class _FakeDedup:
    existing: dict = field(default_factory=dict)

    def find_existing_brief_id(self, table_ref, recipient_email, local_date):
        return self.existing.get((recipient_email, local_date.isoformat()))


@dataclass
class _FakeAuditClient:
    emitted: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.emitted.append((table_ref, rows))
        return []


class _FakeLLM:
    def compose(self, *, prompt: str, model: str) -> str:
        return f"# Morning Brief\n\nRendered for prompt-len={len(prompt)}."


class _StubCalendarFactory:
    def build(self, subject: str):
        class _S:
            def events(self_inner):
                class _E:
                    def list(self_inner2, **kwargs):
                        return self_inner2

                    def execute(self_inner2):
                        return {"items": []}

                return _E()

        return _S()


class _StubGmailFactory:
    def __init__(self) -> None:
        self.last_subject = ""
        self.last_body: dict = {}

    def build(self, subject: str):
        self.last_subject = subject
        outer = self

        class _S:
            def users(self_inner):
                class _U:
                    def drafts(self_inner2):
                        class _D:
                            def create(self_inner3, *, userId, body):
                                outer.last_body = body
                                return self_inner3

                            def execute(self_inner3):
                                return {"id": "r-test-1"}

                        return _D()

                return _U()

        return _S()


def _make_agent(
    *,
    bq_rows: _FakeBQ | None = None,
    bq_writer: _FakeBQRows | None = None,
    dedup: _FakeDedup | None = None,
    gmail_factory: _StubGmailFactory | None = None,
):
    bq_rows = bq_rows or _FakeBQ()
    bq_writer = bq_writer or _FakeBQRows()
    dedup = dedup or _FakeDedup()
    gmail_factory = gmail_factory or _StubGmailFactory()

    audit_bq = _FakeAuditClient()
    audit = AuditLogClient(project_id="p", bq_client=audit_bq)

    composer = MorningBriefComposer(
        prompt_template="Recipient: {{recipient_email}}",
        llm=_FakeLLM(),
    )
    writer = MorningBriefWriter(bq_client=bq_writer, project_id="p", dedup_client=dedup)
    agent = MorningBriefAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        triaged_items_reader=TriagedItemsForOwnerReader(bq_client=bq_rows, project_id="p"),
        open_tasks_reader=OpenTasksForOwnerReader(bq_client=bq_rows, project_id="p"),
        risk_flags_reader=RiskFlagsReader(bq_client=bq_rows, project_id="p"),
        drafts_awaiting_reader=DraftsAwaitingReviewReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=gmail_factory),
        writer=writer,
    )
    return agent, audit_bq, gmail_factory, writer, dedup


def test_run_quiet_day_drafts_a_brief():
    agent, audit_bq, gmail_factory, writer, _ = _make_agent()
    inp = MorningBriefInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    out = agent.invoke(inp)
    assert out.gmail_draft_id == "r-test-1"
    assert out.recipient_email == "owner@example.com"
    assert out.local_date == date(2026, 5, 5)
    assert "Morning Brief" in out.body_markdown
    # The audit row was emitted with success=true.
    assert len(audit_bq.emitted) == 1
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["success"] is True
    assert audit_row["agent_id"] == "morning-brief"
    # Subject in the Gmail draft includes the formatted date.
    import base64

    raw = gmail_factory.last_body["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    # The em-dash is RFC 2047-encoded by Python's email lib into the Subject
    # header. Just check for the date substring + the literal "Morning Brief".
    assert "Morning Brief" in decoded
    assert "Tue, May 05, 2026" in decoded


def test_run_dedup_skip_returns_existing_id_no_gmail_draft():
    dedup = _FakeDedup(existing={("owner@example.com", "2026-05-05"): "existing-77"})

    class _ExplodingGmailFactory:
        def build(self, subject):
            raise RuntimeError("gmail.draft must not be called on dedup-skip")

    agent, audit_bq, _, _, _ = _make_agent(
        dedup=dedup,
        gmail_factory=_ExplodingGmailFactory(),  # type: ignore[arg-type]
    )
    inp = MorningBriefInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    out = agent.invoke(inp)
    assert out.dedup_skipped is True
    assert out.dedup_existing_brief_id == "existing-77"
    assert out.gmail_draft_id is None
    # The audit row reflects the dedup decision.
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["success"] is True
    import json

    output_summary = json.loads(audit_row["output"])
    assert output_summary["dedup_skipped"] is True


def test_run_hipaa_aspect_blocks_invocation():
    from agency_brain.agents.base import HipaaGuardTripped

    agent, audit_bq, _, _, _ = _make_agent()
    inp = MorningBriefInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
        aspects=["hipaa_excluded"],
    )
    try:
        agent.invoke(inp)
    except HipaaGuardTripped:
        pass
    else:
        raise AssertionError("expected HipaaGuardTripped")
    # Audit row was emitted with TRIPPED.
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["hipaa_guard_status"] == "TRIPPED"
    assert audit_row["success"] is False
