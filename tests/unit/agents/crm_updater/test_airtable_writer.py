"""Tests for the Airtable CRM writer — task POST + Pending Updates appends."""

from __future__ import annotations

from agency_brain.agents.crm_updater.airtable_writer import (
    AirtableCRMWriteClient,
    CrmDraftWriter,
    _append_block,
)
from agency_brain.agents.crm_updater.models import (
    AccountMention,
    ContactUpdate,
    ExtractedTask,
)

# --------------------------------------------------------------- helpers


class _StubResponse:
    def __init__(self, *, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


class _StubSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        # Behavior knobs:
        self.next_post_response: _StubResponse | None = None
        self.next_get_response: _StubResponse | None = None
        # Per-table override: maps a substring of the URL (e.g. "/Projects",
        # "/Contacts", "/Accounts") to the response for that GET. Checked
        # first; falls through to ``next_get_response``, then the default.
        # Added 2026-05-11 for the Project-resolution fix — writer now
        # issues GETs to multiple tables per task.
        self.get_responses_by_url_fragment: dict[str, _StubResponse] = {}
        self.next_patch_response: _StubResponse | None = None
        self.headers = {}

    def post(self, url, json=None, timeout=None) -> _StubResponse:
        self.posts.append((url, dict(self.headers), json or {}))
        return self.next_post_response or _StubResponse(
            status_code=200, payload={"id": "rec_new_task"}
        )

    def get(self, url, params=None, timeout=None) -> _StubResponse:
        self.gets.append((url, params or {}))
        for fragment, response in self.get_responses_by_url_fragment.items():
            if fragment in url:
                return response
        return self.next_get_response or _StubResponse(status_code=200, payload={"records": []})

    def patch(self, url, json=None, timeout=None) -> _StubResponse:
        self.patches.append((url, json or {}))
        return self.next_patch_response or _StubResponse(status_code=200)


def _make_client(session: _StubSession) -> AirtableCRMWriteClient:
    return AirtableCRMWriteClient(base_id="appBASE", pat="pat_fake", session=session)


# ----------------------------------------------------------- _append_block


def test_append_block_appends_to_existing() -> None:
    out = _append_block(current="prev block", block="new block")
    assert out == "prev block\n\nnew block"


def test_append_block_handles_empty_current() -> None:
    out = _append_block(current="", block="new block")
    assert out == "new block"
    out2 = _append_block(current=None or "", block="new block")
    assert out2 == "new block"


# --------------------------------------------------------- create_task


def test_create_task_returns_record_id() -> None:
    session = _StubSession()
    session.next_post_response = _StubResponse(status_code=201, payload={"id": "recABC"})
    client = _make_client(session)
    rid = client.create_task({"Task Name": "x", "Approval Status": "Drafted by Agent"})
    assert rid == "recABC"
    assert "/Tasks" in session.posts[0][0]
    body = session.posts[0][2]
    assert body["fields"]["Task Name"] == "x"
    assert body["typecast"] is True


# --------------------------------------------------------- find_contact


def test_find_contact_returns_record_when_present() -> None:
    session = _StubSession()
    session.next_get_response = _StubResponse(
        status_code=200,
        payload={
            "records": [
                {
                    "id": "recCONTACT",
                    "fields": {"Pending Updates": "older block"},
                }
            ]
        },
    )
    client = _make_client(session)
    found = client.find_contact_by_email("sarah@example.com")
    assert found == ("recCONTACT", "older block")
    # Verify formula contains lowercased email.
    params = session.gets[0][1]
    assert "sarah@example.com" in params["filterByFormula"].lower()


def test_find_contact_returns_none_when_absent() -> None:
    session = _StubSession()
    session.next_get_response = _StubResponse(status_code=200, payload={"records": []})
    assert _make_client(session).find_contact_by_email("missing@example.com") is None


def test_find_contact_returns_none_for_empty_email() -> None:
    session = _StubSession()
    assert _make_client(session).find_contact_by_email("") is None
    assert session.gets == []


# --------------------------------------------------------- find_account


def test_find_account_uses_substring_formula() -> None:
    session = _StubSession()
    session.next_get_response = _StubResponse(
        status_code=200,
        payload={"records": [{"id": "recACC", "fields": {}}]},
    )
    client = _make_client(session)
    found = client.find_account_by_name("Acme Corp")
    assert found == ("recACC", "")
    # Substring match via FIND() is used so case-insensitive partials work.
    params = session.gets[0][1]
    assert "FIND(" in params["filterByFormula"]


# --------------------------------------------------------- writer integration


def _writer(session: _StubSession) -> CrmDraftWriter:
    return CrmDraftWriter(
        airtable=_make_client(session),
        inbox_project_record_id="recProject",
        message_id="m1",
    )


def test_writer_creates_tasks_and_appends_pending() -> None:
    session = _StubSession()
    # Default behaviors:
    # - post Tasks: returns rec_new_task (default).
    # - get Contacts: returns one Contact.
    # - patch: 200 (default).
    session.next_get_response = _StubResponse(
        status_code=200,
        payload={"records": [{"id": "recCONTACT", "fields": {"Pending Updates": ""}}]},
    )
    writer = _writer(session)
    result = writer.write(
        tasks=(
            ExtractedTask(
                title="Reply to Sarah",
                due_date="2026-05-15",
                linked_account_name="Acme Corp",
                linked_contact_email="sarah@example.com",
                confidence=0.85,
            ),
        ),
        contact_updates=(
            ContactUpdate(
                contact_email="sarah@example.com",
                last_contact_date="2026-05-09",
                next_followup_suggested=None,
                warmth_change="warmer",
                context_note="Sarah replied positively.",
            ),
        ),
        account_mentions=(),
        run_timestamp_iso="2026-05-09T13:00:00+00:00",
    )
    assert len(result.task_record_ids) == 1
    assert result.contact_updates_appended == 1
    assert result.account_updates_appended == 0

    # Verify the PATCH body contains the timestamped block + the contact email.
    patch_body = session.patches[0][1]
    pending = patch_body["fields"]["Pending Updates"]
    assert "2026-05-09T13:00:00" in pending
    # Source reference is a clickable Gmail permalink, NOT the bare
    # `gmail-msg-<id>` string (which Airtable's text renderer can't
    # auto-link). Bug surfaced 2026-05-11.
    assert "https://mail.google.com/mail/u/0/#all/m1" in pending
    assert "gmail-msg-" not in pending
    assert "sarah@example.com" in pending
    assert "warmer" in pending


def test_writer_skips_unknown_contact() -> None:
    session = _StubSession()
    # GET returns no records — contact not found.
    session.next_get_response = _StubResponse(status_code=200, payload={"records": []})
    writer = _writer(session)
    result = writer.write(
        tasks=(),
        contact_updates=(
            ContactUpdate(
                contact_email="ghost@example.com",
                last_contact_date=None,
                next_followup_suggested=None,
                warmth_change="unchanged",
                context_note="...",
            ),
        ),
        account_mentions=(),
        run_timestamp_iso="2026-05-09T13:00:00+00:00",
    )
    assert result.contact_updates_appended == 0
    assert session.patches == []  # no PATCH issued for missing contact


def test_writer_appends_account_block() -> None:
    session = _StubSession()
    session.next_get_response = _StubResponse(
        status_code=200,
        payload={
            "records": [
                {
                    "id": "recACC",
                    "fields": {"Pending Updates": "previous block"},
                }
            ]
        },
    )
    writer = _writer(session)
    result = writer.write(
        tasks=(),
        contact_updates=(),
        account_mentions=(
            AccountMention(
                account_name="Acme Corp",
                context_note="They're reviewing the Q2 proposal.",
                new_contacts=("billing@acme.com",),
            ),
        ),
        run_timestamp_iso="2026-05-09T13:00:00+00:00",
    )
    assert result.account_updates_appended == 1
    pending = session.patches[0][1]["fields"]["Pending Updates"]
    assert pending.startswith("previous block")
    assert "Suggested New Contacts" in pending
    assert "billing@acme.com" in pending


# ----------------------------------------------------- ADR 0047 Project resolution
# Fix-up: drafted Tasks now resolve linked_account_name → active Project,
# falling back to the configured Triage Inbox only when no unambiguous
# match exists. Original implementation always used inbox.


def test_resolve_project_returns_unique_active_match() -> None:
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(
            status_code=200,
            payload={"records": [{"id": "recPROJ_ACME", "fields": {"Status": "Active"}}]},
        ),
    }
    client = _make_client(session)

    resolved = client.find_active_project_for_account_name("Acme Corp")

    assert resolved == "recPROJ_ACME"
    # Verify formula targets Projects via the Account linked-record field.
    call = next(call for call in session.gets if "/Projects" in call[0])
    formula = call[1]["filterByFormula"]
    assert "FIND(LOWER('Acme Corp'), LOWER({Account})) > 0" in formula


def test_resolve_project_filters_out_terminal_statuses() -> None:
    """Complete / Cancelled / Closed / Archived / Done are denylisted —
    any blank or other status is treated as linkable."""
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(
            status_code=200,
            payload={
                "records": [
                    {"id": "recDONE", "fields": {"Status": "Complete"}},
                    {"id": "recACTIVE", "fields": {"Status": "Active"}},
                    {"id": "recCANCEL", "fields": {"Status": "Cancelled"}},
                ]
            },
        ),
    }
    client = _make_client(session)

    assert client.find_active_project_for_account_name("Acme Corp") == "recACTIVE"


def test_resolve_project_returns_none_when_ambiguous() -> None:
    """Multiple active matches → return None; writer falls back to inbox."""
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(
            status_code=200,
            payload={
                "records": [
                    {"id": "recACTIVE_A", "fields": {"Status": "Active"}},
                    {"id": "recACTIVE_B", "fields": {"Status": "In Progress"}},
                ]
            },
        ),
    }
    client = _make_client(session)

    assert client.find_active_project_for_account_name("Acme") is None


def test_resolve_project_returns_none_for_empty_name() -> None:
    session = _StubSession()
    client = _make_client(session)

    assert client.find_active_project_for_account_name("") is None
    assert client.find_active_project_for_account_name("   ") is None
    # No API call should have happened.
    assert session.gets == []


def test_resolve_project_returns_none_on_api_error() -> None:
    """Any API failure → None (fall back is always safe)."""
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(status_code=500, text="server error"),
    }
    client = _make_client(session)

    assert client.find_active_project_for_account_name("Acme") is None


def test_writer_uses_resolved_project_for_task() -> None:
    """End-to-end: a task with linked_account_name lands its Task in the
    Account's active Project, not the inbox."""
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(
            status_code=200,
            payload={"records": [{"id": "recACME_PROJECT", "fields": {"Status": "Active"}}]},
        ),
    }
    writer = _writer(session)

    writer.write(
        tasks=(
            ExtractedTask(
                title="Reply to Sarah",
                due_date=None,
                linked_account_name="Acme Corp",
                linked_contact_email="sarah@example.com",
                confidence=0.9,
            ),
        ),
        contact_updates=(),
        account_mentions=(),
        run_timestamp_iso="2026-05-11T22:00:00+00:00",
    )

    post_body = session.posts[0][2]
    assert post_body["fields"]["Project"] == ["recACME_PROJECT"]


def test_writer_falls_back_to_inbox_when_no_active_project() -> None:
    """Account with no active Project → Triage Inbox (no broken Task)."""
    session = _StubSession()
    session.get_responses_by_url_fragment = {
        "/Projects": _StubResponse(
            status_code=200,
            payload={"records": [{"id": "recDONE", "fields": {"Status": "Complete"}}]},
        ),
    }
    writer = _writer(session)

    writer.write(
        tasks=(
            ExtractedTask(
                title="Reply to Sarah",
                due_date=None,
                linked_account_name="Acme Corp",
                linked_contact_email=None,
                confidence=0.9,
            ),
        ),
        contact_updates=(),
        account_mentions=(),
        run_timestamp_iso="2026-05-11T22:00:00+00:00",
    )

    post_body = session.posts[0][2]
    assert post_body["fields"]["Project"] == ["recProject"]  # inbox fallback


def test_writer_falls_back_to_inbox_when_no_account_name() -> None:
    """A task without ``linked_account_name`` skips the lookup entirely
    and goes straight to inbox."""
    session = _StubSession()
    writer = _writer(session)

    writer.write(
        tasks=(
            ExtractedTask(
                title="Personal todo",
                due_date=None,
                linked_account_name=None,
                linked_contact_email=None,
                confidence=0.9,
            ),
        ),
        contact_updates=(),
        account_mentions=(),
        run_timestamp_iso="2026-05-11T22:00:00+00:00",
    )

    post_body = session.posts[0][2]
    assert post_body["fields"]["Project"] == ["recProject"]
    # And no Projects GET happened — the resolver short-circuited.
    assert all("/Projects" not in url for url, _params in session.gets)
