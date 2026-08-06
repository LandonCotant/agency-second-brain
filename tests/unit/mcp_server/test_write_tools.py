"""Unit tests for MCP write-tool safety wrappers.

The wrappers are the primary defense against prompt-injected misuse;
these tests assert they actually wrap.
"""

from __future__ import annotations

import hashlib

import pytest
from agency_brain.mcp_server.tools import write as write_tools


class _FakeBQ:
    """Captures issued SQL + returns fixture rows per matcher."""

    def __init__(self, fixtures: list[tuple[str, list[dict]]] | None = None) -> None:
        # fixtures: list of (substring, rows) — first match wins
        self._fixtures = fixtures or []
        self.calls: list[tuple[str, list[dict]]] = []

    def __call__(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        self.calls.append((sql, parameters or []))
        for substr, rows in self._fixtures:
            if substr in sql:
                return rows
        return []


class _FakeEmbedder:
    def __init__(self, vec: list[float] | None = None) -> None:
        self._vec = vec or [0.1] * 768

    def embed(self, *, text: str, model: str = "text-embedding-005") -> list[float]:
        return self._vec


@pytest.fixture(autouse=True)
def _patch_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace BQ + embedder for all tests in this module."""
    fake = _FakeBQ()
    monkeypatch.setattr(write_tools, "query_rows", fake)
    monkeypatch.setattr(write_tools, "embedder", lambda: _FakeEmbedder())
    # Stash fake on the module so tests can replace fixtures as needed.
    write_tools._test_bq = fake  # type: ignore[attr-defined]


# ----------------------------- capture_note ----------------------------------


def test_capture_note_inserts_new_text() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("WHERE note_id =", [])]  # no existing row → insert
    result = write_tools.capture_note("a fresh idea worth remembering")
    assert result["inserted"] is True
    assert result["duplicate"] is False
    assert result["note_id"].startswith("cap-")
    # Two SQL calls: SELECT for dedup, INSERT to write.
    assert len(fake.calls) == 2
    assert "INSERT INTO" in fake.calls[1][0]


def test_capture_note_dedup_returns_duplicate() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    text = "the same note twice"
    note_id = f"cap-{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    fake._fixtures = [("WHERE note_id =", [{"note_id": note_id}])]
    result = write_tools.capture_note(text)
    assert result["inserted"] is False
    assert result["duplicate"] is True
    assert result["note_id"] == note_id
    # Only the SELECT happened; no INSERT.
    assert len(fake.calls) == 1
    assert "INSERT" not in fake.calls[0][0]


def test_capture_note_rejects_empty_text() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.capture_note("   ")
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []  # never touched BQ


# ----------------------------- mark_decision_status --------------------------


def test_mark_decision_status_transitions_drafted_to_confirmed() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT status FROM", [{"status": "drafted"}])]
    result = write_tools.mark_decision_status("dec-abc", "confirmed")
    assert result["updated"] is True
    assert result["prior_status"] == "drafted"
    assert result["new_status"] == "confirmed"


def test_mark_decision_status_noops_when_not_drafted() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT status FROM", [{"status": "confirmed"}])]
    result = write_tools.mark_decision_status("dec-abc", "dismissed")
    assert result["updated"] is False
    assert result["prior_status"] == "confirmed"
    # Only the SELECT happened; no UPDATE.
    assert all("UPDATE" not in sql for sql, _ in fake.calls)


def test_mark_decision_status_rejects_invalid_status() -> None:
    result = write_tools.mark_decision_status("dec-abc", "deleted")
    assert result["updated"] is False
    assert "error" in result


def test_mark_decision_status_missing_decision() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT status FROM", [])]
    result = write_tools.mark_decision_status("dec-missing", "confirmed")
    assert result["updated"] is False
    assert result["prior_status"] is None


def test_mark_decision_status_does_not_set_nonexistent_column() -> None:
    """Bug 4 regression check: the UPDATE must use `refined_at`, not
    `status_updated_at` (which doesn't exist on decisions)."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT status FROM", [{"status": "drafted"}])]
    write_tools.mark_decision_status("dec-abc", "confirmed")
    update_sql = next(sql for sql, _ in fake.calls if "UPDATE" in sql)
    assert "status_updated_at" not in update_sql
    assert "refined_at" in update_sql


# ----------------------------- insert_decision -------------------------------


def test_insert_decision_forces_drafted_status() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.insert_decision("decided to ship the MCP server first")
    assert result["inserted"] is True
    assert result["decision_id"].startswith("dec-")
    insert_sql = fake.calls[0][0]
    assert "INSERT INTO" in insert_sql
    assert "'drafted'" in insert_sql


def test_insert_decision_populates_required_columns() -> None:
    """Bug 4 regression check: the decisions table's REQUIRED columns
    must all be populated, and the INSERT must not reference columns
    that don't exist on the real schema."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    write_tools.insert_decision("we should pursue partnership #2 next quarter")
    insert_sql = fake.calls[0][0]
    column_list = insert_sql.split("VALUES")[0]
    # REQUIRED columns per the live decisions schema.
    for required_col in (
        "decision_id",
        "decided_at",
        "title",
        "choice",
        "status",
        "review_30_at",
        "review_90_at",
        "review_365_at",
    ):
        assert (
            required_col in column_list
        ), f"INSERT missing REQUIRED decisions column {required_col!r}"
    # Columns the buggy version referenced but DON'T exist:
    for nonexistent in ("content", "status_updated_at"):
        assert (
            nonexistent not in column_list
        ), f"INSERT references nonexistent decisions column {nonexistent!r}"
    # `source` is a column name in the OLD buggy INSERT and is also a
    # substring of `source_reflection_id`, so do a stricter check.
    assert " source," not in column_list and " source)" not in column_list


def test_insert_decision_derives_title_from_first_line() -> None:
    """Title is the first line truncated to 80 chars. choice carries
    the full text."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    write_tools.insert_decision("Buy the Mac Studio.\n\nLong rationale here.")
    _, params = fake.calls[0]
    pdict = {p["name"]: p["value"] for p in params}
    assert pdict["title"] == "Buy the Mac Studio."
    assert "Long rationale" in pdict["choice"]


def test_insert_decision_rejects_empty_text() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.insert_decision("")
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []


# ----------------------------- insert_win ------------------------------------


def test_insert_win_dedups_on_deterministic_win_id() -> None:
    """Dedup is via the deterministic win_id (mcp-{week_of}-{title_hash12}),
    NOT a title_hash12 column — that column doesn't exist on the real
    wins table (Bug 4 root cause)."""
    from datetime import UTC, datetime, timedelta

    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    title = "shipped the MCP server"
    title_hash12 = hashlib.sha256(title.lower().encode()).hexdigest()[:12]
    today = datetime.now(UTC).date()
    week_of = today - timedelta(days=today.weekday())
    expected_win_id = f"mcp-{week_of.isoformat()}-{title_hash12}"
    fake._fixtures = [("SELECT win_id FROM", [{"win_id": expected_win_id}])]
    result = write_tools.insert_win(title=title, context="big day")
    assert result["inserted"] is False
    assert result["duplicate"] is True
    assert result["win_id"] == expected_win_id
    # Dedup SELECT must use win_id, NOT the nonexistent title_hash12 column.
    dedup_sql, dedup_params = fake.calls[0]
    assert "WHERE win_id = @wid" in dedup_sql
    assert "title_hash12" not in dedup_sql


def test_insert_win_inserts_new() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT win_id FROM", [])]
    result = write_tools.insert_win(title="brand new win", context="context")
    assert result["inserted"] is True
    assert result["duplicate"] is False
    assert result["win_id"].startswith("mcp-")
    # Four calls: wins SELECT (dedup) + wins INSERT + synthetic-note SELECT
    # (dedup) + synthetic-note INSERT. Per ADR 0052, every successful
    # win write also writes a companion notes row for brain_ask.
    assert len(fake.calls) == 4
    wins_insert_sql = fake.calls[1][0]
    assert "INSERT INTO" in wins_insert_sql
    # Real wins schema columns — assertions guard against the
    # column-name regression that motivated Bug 4.
    assert "captured_at" in wins_insert_sql
    assert "week_of" in wins_insert_sql
    assert "source_kind" in wins_insert_sql
    assert "summary" in wins_insert_sql
    assert "'mcp'" in wins_insert_sql  # source_kind literal
    # Columns that the buggy version referenced but DON'T exist:
    assert "title_hash12" not in wins_insert_sql
    assert "context" not in wins_insert_sql.split("VALUES")[0]
    assert "created_at" not in wins_insert_sql.split("VALUES")[0]
    # Synthetic-note INSERT lands in the notes table with note_kind='win'.
    notes_insert_sql = fake.calls[3][0]
    assert "INSERT INTO" in notes_insert_sql
    assert "agent_outputs.notes" in notes_insert_sql


def test_insert_win_omits_nonexistent_columns_when_called() -> None:
    """The Bug 4 regression check: the INSERT must not reference
    `title_hash12`, `context`, or `created_at` (none of these are
    columns on agent_outputs.wins)."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT win_id FROM", [])]
    write_tools.insert_win(title="some win", context="some context")
    insert_sql = fake.calls[1][0]
    column_list = insert_sql.split("VALUES")[0]
    for nonexistent in ("title_hash12", "created_at"):
        assert (
            nonexistent not in column_list
        ), f"INSERT references nonexistent wins column {nonexistent!r}"


def test_insert_win_dedup_case_insensitive() -> None:
    """Title hash uses .lower() so case variation collapses to the same row."""
    title_lower = "won the contract"
    title_upper = "WON THE Contract"
    h_lower = hashlib.sha256(title_lower.lower().encode()).hexdigest()[:12]
    h_upper = hashlib.sha256(title_upper.lower().encode()).hexdigest()[:12]
    assert h_lower == h_upper


def test_insert_win_rejects_empty_title() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.insert_win(title="")
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []


# ----------------------------- synthetic notes (ADR 0052) -------------------


def test_insert_decision_writes_synthetic_note() -> None:
    """ADR 0052: every successful decision write also writes a companion
    notes row with note_kind='decision' so brain_ask can find it."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.insert_decision("Switch to weekly briefs")
    assert result["inserted"] is True
    # The result advertises the synthetic note id.
    assert result["synthetic_note_id"] is not None
    assert result["synthetic_note_id"].startswith("syn-dec-")
    assert result["synthetic_note_error"] is None
    # Should have: decisions INSERT + notes SELECT (dedup) + notes INSERT.
    assert len(fake.calls) == 3
    notes_insert_sql = fake.calls[2][0]
    assert "agent_outputs.notes" in notes_insert_sql
    # Confirm the synthetic note carries note_kind='decision' as a param.
    notes_insert_params = {p["name"]: p["value"] for p in fake.calls[2][1]}
    assert notes_insert_params["note_kind"] == "decision"
    assert notes_insert_params["extraction_method"] == "synthetic-decision-v1"


def test_insert_win_writes_synthetic_note_with_correct_note_kind() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT win_id FROM", [])]
    result = write_tools.insert_win(title="shipped ADR 0052", context="cool")
    assert result["synthetic_note_id"].startswith("syn-win-")
    assert result["synthetic_note_error"] is None
    notes_insert_params = {p["name"]: p["value"] for p in fake.calls[3][1]}
    assert notes_insert_params["note_kind"] == "win"
    assert notes_insert_params["extraction_method"] == "synthetic-win-v1"


def test_insert_synthetic_note_is_idempotent_on_existing_note_id() -> None:
    """Second call with same note_id (e.g. backfill rerun) returns
    (False, None) — no re-embed, no re-insert."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT note_id FROM", [{"note_id": "syn-dec-existing"}])]
    inserted, err = write_tools._insert_synthetic_note(
        note_id="syn-dec-existing",
        note_kind="decision",
        source_record_id="dec-existing",
        title="Already backfilled",
        body="...",
        revision_id="2026-05-14T00:00:00+00:00",
        extraction_method="synthetic-decision-v1-backfill",
    )
    assert inserted is False
    assert err is None
    # Only the dedup SELECT happened; no INSERT.
    assert len(fake.calls) == 1
    assert "SELECT note_id FROM" in fake.calls[0][0]


def test_insert_synthetic_note_embed_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the embedder fails, the helper logs and returns (False, err)
    instead of bubbling. The parent decision/win write must not be
    rolled back by an embed-side hiccup."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]

    class _BrokenEmbedder:
        def embed(self, *, text: str, model: str = "text-embedding-005") -> list[float]:
            raise RuntimeError("vertex transient 500")

    monkeypatch.setattr(write_tools, "embedder", lambda: _BrokenEmbedder())
    inserted, err = write_tools._insert_synthetic_note(
        note_id="syn-dec-tx",
        note_kind="decision",
        source_record_id="dec-tx",
        title="t",
        body="b",
        revision_id="2026-05-14T00:00:00+00:00",
        extraction_method="synthetic-decision-v1",
    )
    assert inserted is False
    assert err is not None
    assert "embed_failed" in err
    # Only the dedup SELECT happened; the INSERT never fired.
    assert len(fake.calls) == 1


def test_insert_decision_continues_when_synthetic_note_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical decisions row must still land even if the
    synthetic-note write fails."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]

    class _BrokenEmbedder:
        def embed(self, *, text: str, model: str = "text-embedding-005") -> list[float]:
            raise RuntimeError("vertex down")

    monkeypatch.setattr(write_tools, "embedder", lambda: _BrokenEmbedder())
    result = write_tools.insert_decision("Pivot to weekly cadence")
    assert result["inserted"] is True
    assert result["decision_id"].startswith("dec-")
    assert result["synthetic_note_id"] is None
    assert result["synthetic_note_error"] is not None
    # decisions INSERT did happen.
    decisions_insert_sql = fake.calls[0][0]
    assert "agent_outputs.decisions" in decisions_insert_sql


# ----------------------------- record_feedback (ADR 0060) --------------------


def test_record_feedback_rejects_invalid_scope() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.record_feedback(scope="bogus", verdict="noise")
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []  # never touched BQ


def test_record_feedback_rejects_invalid_verdict() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.record_feedback(scope="risk", verdict="meh")
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []


def test_record_feedback_rejects_nonpositive_mute_days() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    result = write_tools.record_feedback(
        scope="risk", verdict="noise", account_name="Acme", mute_days=0
    )
    assert result["inserted"] is False
    assert "error" in result
    assert fake.calls == []


def test_record_feedback_account_not_found() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("airtable_replica.accounts", [])]  # no match
    result = write_tools.record_feedback(
        scope="risk", verdict="noise", account_name="Nonexistent Co"
    )
    assert result["inserted"] is False
    assert "no account matched" in result["error"]
    # Only the resolution SELECT fired; no dedup, no INSERT.
    assert len(fake.calls) == 1


def test_record_feedback_ambiguous_account() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        (
            "airtable_replica.accounts",
            [{"_airtable_record_id": "recA"}, {"_airtable_record_id": "recB"}],
        )
    ]
    result = write_tools.record_feedback(scope="risk", verdict="noise", account_name="Smith")
    assert result["inserted"] is False
    assert "ambiguous" in result["error"]
    assert len(fake.calls) == 1


def test_record_feedback_noise_inserts_and_resolves_account() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        ("airtable_replica.accounts", [{"_airtable_record_id": "recAcme"}]),
        ("SELECT feedback_id FROM", []),  # no recent dup → insert
    ]
    result = write_tools.record_feedback(
        scope="risk",
        verdict="noise",
        account_name="Acme",
        pattern_name="acknowledgment_gap",
        mute_days=30,
    )
    assert result["inserted"] is True
    assert result["duplicate"] is False
    assert result["feedback_id"].startswith("fb-")
    assert result["account_id"] == "recAcme"
    assert result["mute_until"] is not None
    # Three SQL calls: account resolve, dedup SELECT, INSERT.
    assert len(fake.calls) == 3
    insert_sql, insert_params = fake.calls[2]
    assert "INSERT INTO" in insert_sql
    assert "signal_feedback" in insert_sql
    # created_by is forced to 'operator' as a SQL literal — never a param.
    assert "'operator'" in insert_sql
    pdict = {p["name"]: p["value"] for p in insert_params}
    assert pdict["scope"] == "risk"
    assert pdict["verdict"] == "noise"
    assert pdict["account_id"] == "recAcme"
    assert pdict["pattern_name"] == "acknowledgment_gap"
    assert pdict["mute_until"] is not None


def test_record_feedback_dedup_within_60s_returns_duplicate() -> None:
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        ("airtable_replica.accounts", [{"_airtable_record_id": "recAcme"}]),
        ("SELECT feedback_id FROM", [{"feedback_id": "fb-existing12345"}]),
    ]
    result = write_tools.record_feedback(
        scope="risk", verdict="noise", account_name="Acme", pattern_name="acknowledgment_gap"
    )
    assert result["inserted"] is False
    assert result["duplicate"] is True
    assert result["feedback_id"] == "fb-existing12345"
    # account resolve + dedup SELECT only; no INSERT.
    assert len(fake.calls) == 2
    assert all("INSERT" not in sql for sql, _ in fake.calls)


def test_record_feedback_account_agnostic_skips_resolution() -> None:
    """Draft-tone feedback with no account_name → account_id NULL, no
    resolution query."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [("SELECT feedback_id FROM", [])]
    result = write_tools.record_feedback(scope="draft", verdict="wrong_tone", note="too stiff")
    assert result["inserted"] is True
    assert result["account_id"] is None
    # No account-resolution SELECT — first call is the dedup SELECT.
    assert "airtable_replica.accounts" not in fake.calls[0][0]
    assert len(fake.calls) == 2  # dedup SELECT + INSERT
    pdict = {p["name"]: p["value"] for p in fake.calls[1][1]}
    assert pdict["account_id"] is None
    assert pdict["verdict"] == "wrong_tone"


def test_record_feedback_mute_until_only_for_noise() -> None:
    """A non-noise verdict never sets mute_until even if mute_days slips in."""
    fake: _FakeBQ = write_tools._test_bq  # type: ignore[attr-defined]
    fake._fixtures = [
        ("airtable_replica.accounts", [{"_airtable_record_id": "recAcme"}]),
        ("SELECT feedback_id FROM", []),
    ]
    result = write_tools.record_feedback(
        scope="risk", verdict="valid", account_name="Acme", mute_days=30
    )
    assert result["inserted"] is True
    assert result["mute_until"] is None
    pdict = {p["name"]: p["value"] for p in fake.calls[2][1]}
    assert pdict["mute_until"] is None
