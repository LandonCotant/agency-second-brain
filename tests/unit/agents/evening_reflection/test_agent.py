"""Unit tests for EveningReflectionAgent.

The agent orchestrates 5 readers + composer + Gmail drafts + writer
dedup. Each dependency is replaced with a tiny fake so the test stays
fast and deterministic. The audit log + memory bank come from the
production utilities (in-memory variants).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agency_brain.agents.evening_reflection.agent import EveningReflectionAgent
from agency_brain.agents.evening_reflection.composer import (
    EveningReflectionComposer,
)
from agency_brain.agents.evening_reflection.models import EveningReflectionInput
from agency_brain.agents.evening_reflection.readers import (
    ActiveRiskFlagsTodayReader,
    CompletedTasksTodayReader,
    MorningBriefForTodayReader,
    TriagedItemsTodayReader,
)
from agency_brain.agents.evening_reflection.writer import (
    EveningReflectionWriter,
)
from agency_brain.agents.morning_brief.calendar_client import CalendarClient
from agency_brain.agents.morning_brief.gmail_drafts_client import (
    GmailDraftsClient,
)
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

    def find_existing_reflection_id(self, table_ref, recipient_email, local_date, mode="reflect"):
        # Tests can key on (recipient, date) for backward compat OR on
        # (recipient, date, mode) for ADR 0040 §7 mode-aware dedup
        # cases. Look up the more specific key first.
        triple = (recipient_email, local_date.isoformat(), mode)
        if triple in self.existing:
            return self.existing[triple]
        return self.existing.get((recipient_email, local_date.isoformat()))


@dataclass
class _FakeAuditClient:
    emitted: list = field(default_factory=list)

    def insert_rows_json(self, table_ref, rows):
        self.emitted.append((table_ref, rows))
        return []


class _FakeLLM:
    def compose(self, *, prompt: str, model: str) -> str:
        return (
            f"### What happened today\n\nRendered for prompt-len={len(prompt)}.\n\n"
            "### What it might mean\n\nQuiet day.\n\n"
            "### Worth carrying into tomorrow\n\nWhat to pull forward?"
        )


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
                                return {"id": "r-test-er"}

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

    composer = EveningReflectionComposer(
        prompt_template="Recipient: {{recipient_email}}",
        llm=_FakeLLM(),
    )
    writer = EveningReflectionWriter(bq_client=bq_writer, project_id="p", dedup_client=dedup)
    agent = EveningReflectionAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        completed_tasks_reader=CompletedTasksTodayReader(bq_client=bq_rows, project_id="p"),
        triaged_today_reader=TriagedItemsTodayReader(bq_client=bq_rows, project_id="p"),
        morning_brief_reader=MorningBriefForTodayReader(bq_client=bq_rows, project_id="p"),
        active_risk_flags_reader=ActiveRiskFlagsTodayReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=gmail_factory),
        writer=writer,
    )
    return agent, audit_bq, gmail_factory, writer, dedup


def test_run_unremarkable_day_drafts_a_reflection():
    agent, audit_bq, gmail_factory, writer, _ = _make_agent()
    inp = EveningReflectionInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    out = agent.invoke(inp)
    assert out.gmail_draft_id == "r-test-er"
    assert out.recipient_email == "owner@example.com"
    assert out.local_date == date(2026, 5, 5)
    assert "What happened today" in out.body_markdown
    # Audit row emitted with success=true and the agent_id.
    assert len(audit_bq.emitted) == 1
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["success"] is True
    assert audit_row["agent_id"] == "evening-reflection"
    # Gmail subject formatted with the date.
    import base64

    raw = gmail_factory.last_body["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    # The em-dash in the subject is RFC 2047-encoded, but the date and
    # the literal "Evening Reflection" come through.
    assert "Evening Reflection" in decoded
    assert "Tue, May 05, 2026" in decoded


def test_run_dedup_skip_returns_existing_id_no_gmail_draft():
    dedup = _FakeDedup(existing={("owner@example.com", "2026-05-05"): "existing-er-77"})

    class _ExplodingGmailFactory:
        def build(self, subject):
            raise RuntimeError("gmail.draft must not be called on dedup-skip")

    agent, audit_bq, _, _, _ = _make_agent(
        dedup=dedup,
        gmail_factory=_ExplodingGmailFactory(),  # type: ignore[arg-type]
    )
    inp = EveningReflectionInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    out = agent.invoke(inp)
    assert out.dedup_skipped is True
    assert out.dedup_existing_reflection_id == "existing-er-77"
    assert out.gmail_draft_id is None
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["success"] is True
    import json

    output_summary = json.loads(audit_row["output"])
    assert output_summary["dedup_skipped"] is True
    assert output_summary["dedup_existing_reflection_id"] == "existing-er-77"


def test_run_hipaa_aspect_blocks_invocation():
    from agency_brain.agents.base import HipaaGuardTripped

    agent, audit_bq, _, _, _ = _make_agent()
    inp = EveningReflectionInput(
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
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["hipaa_guard_status"] == "TRIPPED"
    assert audit_row["success"] is False


def test_run_with_data_uses_all_sections():
    """When all readers return data, the audit row's sections_used reflects them."""
    bq_rows = _FakeBQ(
        rows=[
            # First call is whichever reader runs first; queue real rows.
            # Each load() call resets to the pre-populated rows because our
            # _FakeBQ returns a copy. To exercise sections_used, we feed
            # one row to each call by setting rows once and letting all
            # five readers see the same single row — the test only checks
            # that the agent records the readers that returned non-empty.
            {
                "task_id": "t-1",
                "name": "X",
                "project_name": None,
                "completed_date": "2026-05-05",
                "item_id": "i-1",
                "severity": "info",
                "summary": "FYI",
                "source": "gmail",
                "action_type": "wait",
                "source_url": None,
                "brief_id": "b-1",
                "body_markdown": "# B",
                "sections_used": [],
                "flag_id": "f-1",
                "pattern_name": "P",
                "reasoning": None,
                "account_name": None,
            }
        ]
    )
    agent, audit_bq, _, _, _ = _make_agent(bq_rows=bq_rows)
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
        )
    )
    assert out.dedup_skipped is False
    # All four BQ readers + morning_brief loaded successfully (calendar
    # is stubbed empty).
    assert "completed_tasks" in out.sections_used
    assert "triaged_today" in out.sections_used
    assert "morning_brief" in out.sections_used
    assert "active_risk_flags" in out.sections_used
    # Calendar was empty in the stub.
    assert "calendar" not in out.sections_used


def test_run_tolerates_failing_reader():
    """One reader raising must not block the reflection."""

    class _ExplodingBQ:
        last_sql = ""

        def query_rows(self, sql):
            raise RuntimeError("BQ down")

    bq_rows = _ExplodingBQ()
    bq_writer = _FakeBQRows()
    dedup = _FakeDedup()

    audit_bq = _FakeAuditClient()
    audit = AuditLogClient(project_id="p", bq_client=audit_bq)
    composer = EveningReflectionComposer(prompt_template="x", llm=_FakeLLM())
    writer = EveningReflectionWriter(bq_client=bq_writer, project_id="p", dedup_client=dedup)
    gmail_factory = _StubGmailFactory()
    agent = EveningReflectionAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        completed_tasks_reader=CompletedTasksTodayReader(bq_client=bq_rows, project_id="p"),
        triaged_today_reader=TriagedItemsTodayReader(bq_client=bq_rows, project_id="p"),
        morning_brief_reader=MorningBriefForTodayReader(bq_client=bq_rows, project_id="p"),
        active_risk_flags_reader=ActiveRiskFlagsTodayReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=gmail_factory),
        writer=writer,
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
        )
    )
    # Reader failures don't break the run; sections_used is empty but we
    # still produce a draft.
    assert out.gmail_draft_id == "r-test-er"
    assert out.sections_used == ()
    audit_row = audit_bq.emitted[0][1][0]
    assert audit_row["success"] is True


def test_audit_input_summary_carries_recipient_and_date():
    agent, audit_bq, _, _, _ = _make_agent()
    inp = EveningReflectionInput(
        recipient_email="owner@example.com",
        run_date=date(2026, 5, 5),
    )
    agent.invoke(inp)
    import json

    audit_row = audit_bq.emitted[0][1][0]
    input_summary = json.loads(audit_row["input_summary"])
    assert input_summary["recipient_email"] == "owner@example.com"
    assert input_summary["run_date"] == "2026-05-05"


# ------------------------------------------ REFLECT-mode structured path (ADR 0040)


def test_reflect_mode_with_structured_path_extracts_and_dispatches():
    """End-to-end PR-B path: structured composer returns a payload, the
    agent dispatches decisions/wins/todos, the Gmail draft body =
    commentary + summary block, audit row carries extraction counts."""
    import base64
    import json

    from agency_brain.agents.evening_reflection.extracted_writers import (
        DecisionsWriter,
        WinsWriter,
    )
    from agency_brain.agents.evening_reflection.models import (
        ReflectionMode,
    )
    from agency_brain.agents.evening_reflection.triage_publisher import (
        ReflectTriagePublisher,
    )

    # Stub structured LLM returns a canned JSON payload.
    structured_json = json.dumps(
        {
            "commentary": "Today held real signal.",
            "decisions": [
                {
                    "title": "Renew Client A",
                    "context": "Q3 expires soon",
                    "source_voice_note_id": "captures-recA",
                }
            ],
            "wins": [
                {"title": "Closed ClientC Q3", "summary": "Done", "source_voice_note_id": None}
            ],
            "todos": [{"body": "Email Alice about brief", "source_voice_note_id": None}],
        }
    )

    class _StubStructuredLLM:
        def generate(self, *, prompt: str, model: str | None = None) -> str:
            return structured_json

    @dataclass
    class _BQQueryStub:
        existing: set = field(default_factory=set)
        captured_sqls: list = field(default_factory=list)

        def query_rows(self, sql, parameters=None):
            self.captured_sqls.append((sql, parameters))
            if not parameters:
                return []
            return [{"x": 1}] if parameters[0]["value"] in self.existing else []

    @dataclass
    class _BQInsertStub:
        captured: list = field(default_factory=list)

        def insert_rows_json(self, table_ref, rows):
            self.captured.append((table_ref, rows))
            return []

    @dataclass
    class _PubStub:
        publishes: list = field(default_factory=list)

        def publish(self, topic, data, ordering_key="", **attrs):
            self.publishes.append((topic, data, ordering_key))

            class _Fut:
                def result(self_inner, timeout=None):
                    return None

            return _Fut()

    bq_rows = _FakeBQ()
    bq_writer = _FakeBQRows()
    dedup = _FakeDedup()
    audit_bq = _FakeAuditClient()
    audit = AuditLogClient(project_id="p", bq_client=audit_bq)

    bq_insert_stub = _BQInsertStub()
    bq_query_stub = _BQQueryStub()
    pub_stub = _PubStub()

    composer = EveningReflectionComposer(
        prompt_template="REFLECT prompt: {{voice_memos_block}}",
        llm=_FakeLLM(),
        structured_llm=_StubStructuredLLM(),
    )
    writer = EveningReflectionWriter(bq_client=bq_writer, project_id="p", dedup_client=dedup)
    decisions_writer = DecisionsWriter(
        bq=bq_insert_stub,
        bq_query=bq_query_stub,
        table_ref="p.agent_outputs.decisions",
    )
    wins_writer = WinsWriter(
        bq=bq_insert_stub,
        bq_query=bq_query_stub,
        table_ref="p.agent_outputs.wins",
    )
    triage_publisher = ReflectTriagePublisher(
        publisher=pub_stub,
        topic_path="projects/p/topics/asb-triage-input",
    )
    gmail_factory = _StubGmailFactory()
    agent = EveningReflectionAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        completed_tasks_reader=CompletedTasksTodayReader(bq_client=bq_rows, project_id="p"),
        triaged_today_reader=TriagedItemsTodayReader(bq_client=bq_rows, project_id="p"),
        morning_brief_reader=MorningBriefForTodayReader(bq_client=bq_rows, project_id="p"),
        active_risk_flags_reader=ActiveRiskFlagsTodayReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=gmail_factory),
        writer=writer,
        decisions_writer=decisions_writer,
        wins_writer=wins_writer,
        triage_publisher=triage_publisher,
    )

    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.REFLECT,
        )
    )

    # Body = commentary + dispatch summary block (PR-B contract).
    assert "Today held real signal." in out.body_markdown
    assert "Decisions extracted" in out.body_markdown
    assert "Renew Client A" in out.body_markdown
    assert "Closed ClientC Q3" in out.body_markdown
    assert "Email Alice about brief" in out.body_markdown

    # Sections_used carries the new structured_extraction marker.
    assert "structured_extraction" in out.sections_used

    # Decisions + wins INSERTed; todos published.
    inserted_tables = [t for t, _ in bq_insert_stub.captured]
    assert "p.agent_outputs.decisions" in inserted_tables
    assert "p.agent_outputs.wins" in inserted_tables
    assert len(pub_stub.publishes) == 1

    # Audit row's output summary surfaces extraction counts.
    audit_row = audit_bq.emitted[0][1][0]
    output_summary = json.loads(audit_row["output"])
    assert output_summary["extracted_decisions_written"] == 1
    assert output_summary["extracted_wins_written"] == 1
    assert output_summary["extracted_todos_published"] == 1

    # Gmail subject still uses Evening Reflection.
    raw = gmail_factory.last_body["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "Evening Reflection" in decoded


def test_reflect_mode_falls_back_to_prose_when_structured_compose_fails():
    """If the structured LLM returns garbage JSON, the agent must NOT
    blow up — it falls back to the prose composer so the daily Gmail
    draft still ships. No decisions/wins land."""
    from agency_brain.agents.evening_reflection.extracted_writers import (
        DecisionsWriter,
        WinsWriter,
    )
    from agency_brain.agents.evening_reflection.models import ReflectionMode
    from agency_brain.agents.evening_reflection.triage_publisher import (
        ReflectTriagePublisher,
    )

    class _BadStructuredLLM:
        def generate(self, *, prompt: str, model: str | None = None) -> str:
            return "this is not json at all"

    @dataclass
    class _BQQueryStub:
        def query_rows(self, sql, parameters=None):
            return []

    @dataclass
    class _BQInsertStub:
        captured: list = field(default_factory=list)

        def insert_rows_json(self, table_ref, rows):
            self.captured.append((table_ref, rows))
            return []

    @dataclass
    class _PubStub:
        publishes: list = field(default_factory=list)

        def publish(self, topic, data, ordering_key="", **attrs):
            self.publishes.append((topic, data, ordering_key))

            class _Fut:
                def result(self_inner, timeout=None):
                    return None

            return _Fut()

    bq_rows = _FakeBQ()
    bq_writer = _FakeBQRows()
    bq_insert_stub = _BQInsertStub()
    pub_stub = _PubStub()
    audit_bq = _FakeAuditClient()
    audit = AuditLogClient(project_id="p", bq_client=audit_bq)

    composer = EveningReflectionComposer(
        prompt_template="REFLECT: {{voice_memos_block}}",
        llm=_FakeLLM(),
        structured_llm=_BadStructuredLLM(),
    )
    writer = EveningReflectionWriter(bq_client=bq_writer, project_id="p", dedup_client=_FakeDedup())
    decisions_writer = DecisionsWriter(
        bq=bq_insert_stub,
        bq_query=_BQQueryStub(),
        table_ref="p.agent_outputs.decisions",
    )
    wins_writer = WinsWriter(
        bq=bq_insert_stub,
        bq_query=_BQQueryStub(),
        table_ref="p.agent_outputs.wins",
    )
    agent = EveningReflectionAgent(
        sa_email="asb-agent-triage-sa@p.iam.gserviceaccount.com",
        audit_log=audit,
        memory_bank=InMemoryMemoryBank(),
        composer=composer,
        completed_tasks_reader=CompletedTasksTodayReader(bq_client=bq_rows, project_id="p"),
        triaged_today_reader=TriagedItemsTodayReader(bq_client=bq_rows, project_id="p"),
        morning_brief_reader=MorningBriefForTodayReader(bq_client=bq_rows, project_id="p"),
        active_risk_flags_reader=ActiveRiskFlagsTodayReader(bq_client=bq_rows, project_id="p"),
        calendar_client=CalendarClient(service_factory=_StubCalendarFactory()),
        gmail_drafts_client=GmailDraftsClient(service_factory=_StubGmailFactory()),
        writer=writer,
        decisions_writer=decisions_writer,
        wins_writer=wins_writer,
        triage_publisher=ReflectTriagePublisher(
            publisher=pub_stub, topic_path="projects/p/topics/asb-triage-input"
        ),
    )
    out = agent.invoke(
        EveningReflectionInput(
            recipient_email="owner@example.com",
            run_date=date(2026, 5, 5),
            mode=ReflectionMode.REFLECT,
        )
    )
    # Prose composer fallback drove the body.
    assert "What happened today" in out.body_markdown
    # No decisions/wins/todos landed.
    assert bq_insert_stub.captured == []
    assert pub_stub.publishes == []
    # No structured_extraction marker on a fallback run.
    assert "structured_extraction" not in out.sections_used
    # Audit still records success — the daily ritual shipped.
    assert audit_bq.emitted[0][1][0]["success"] is True
