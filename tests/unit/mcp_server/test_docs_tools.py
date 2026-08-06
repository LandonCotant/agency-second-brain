"""Unit tests for the docs.update_weekly_doc tool.

Patches drive_client / docs_client with fakes so no Drive API calls
happen. Verifies: doc title computation, find-or-create, batchUpdate
shape (insertText + HEADING_1 style), idempotency on same-day rerun,
input validation, env-var resolution.
"""

from __future__ import annotations

import pytest
from agency_brain.mcp_server.tools import docs as docs_tools


class _FakeDocsAPI:
    def __init__(self) -> None:
        self.batch_updates: list[tuple[str, dict]] = []

    def documents(self) -> _FakeDocsAPI:
        return self

    def batchUpdate(self, *, documentId: str, body: dict):
        self.batch_updates.append((documentId, body))
        return _FakeExec({"documentId": documentId})


class _FakeDriveAPI:
    def __init__(
        self,
        *,
        existing_files: dict[tuple[str, str], str] | None = None,
        export_text: str = "",
    ) -> None:
        # Keys: (folder_id, title). Values: file_id.
        self._existing = existing_files or {}
        self._export_text = export_text
        self.created: list[dict] = []
        self.exports: list[str] = []

    def files(self) -> _FakeDriveAPI:
        return self

    def list(self, *, q: str, fields: str, pageSize: int, **_):
        # Parse q for name=... and parent=...
        # Format example:
        #   name = 'X' and 'FOLDER' in parents and mimeType = '...' and trashed = false
        import re

        name_match = re.search(r"name = '([^']+)'", q)
        parent_match = re.search(r"'([^']+)' in parents", q)
        if not (name_match and parent_match):
            return _FakeExec({"files": []})
        title = name_match.group(1).replace("\\'", "'")
        folder = parent_match.group(1)
        fid = self._existing.get((folder, title))
        return _FakeExec({"files": [{"id": fid, "name": title}] if fid else []})

    def create(self, *, body: dict, fields: str, **_):
        self.created.append(body)
        fake_id = f"fake-{len(self.created)}"
        # Register the just-created file so future _find_doc would see it.
        parent = body["parents"][0]
        title = body["name"]
        self._existing[(parent, title)] = fake_id
        return _FakeExec({"id": fake_id})

    def export(self, *, fileId: str, mimeType: str):
        self.exports.append(fileId)
        return _FakeExec(self._export_text.encode("utf-8"))


class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


@pytest.fixture(autouse=True)
def _clear_factory_caches():
    """Ensure each test sees a fresh patched client."""
    docs_tools.drive_client.cache_clear()
    docs_tools.docs_client.cache_clear()


@pytest.fixture
def patched_clients(monkeypatch: pytest.MonkeyPatch):
    """Patch the lazy-import factories and return (drive, docs) fakes.

    Default: no existing files in any folder; export_text empty.
    Tests can mutate the fakes via the returned references.
    """
    drive = _FakeDriveAPI()
    docs = _FakeDocsAPI()
    monkeypatch.setattr(docs_tools, "drive_client", lambda: drive)
    monkeypatch.setattr(docs_tools, "docs_client", lambda: docs)
    return drive, docs


@pytest.fixture(autouse=True)
def _set_folder_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_BRIEFS_FOLDER_ID", "folder-briefs")
    monkeypatch.setenv("BRAIN_REFLECTIONS_FOLDER_ID", "folder-reflections")
    monkeypatch.setenv("BRAIN_REVIEWS_FOLDER_ID", "folder-reviews")


# ----------------------------- title + folder logic --------------------------


def test_update_weekly_doc_creates_new_doc_when_missing(patched_clients) -> None:
    drive, docs = patched_clients
    result = docs_tools.update_weekly_doc(
        kind="brief", date="2026-05-14", section_markdown="Today's brief content."
    )
    assert result["created"] is True
    assert result["updated"] is True
    assert result["duplicate"] is False
    # 2026-05-14 is a Thursday; Monday of that week is 2026-05-11.
    assert result["anchor_date"] == "2026-05-11"
    assert len(drive.created) == 1
    assert drive.created[0]["name"] == "Morning Briefs — Week of 2026-05-11"
    assert drive.created[0]["parents"] == ["folder-briefs"]
    assert drive.created[0]["mimeType"] == "application/vnd.google-apps.document"


def test_update_weekly_doc_reflection_uses_reflections_folder(patched_clients) -> None:
    drive, _ = patched_clients
    result = docs_tools.update_weekly_doc(
        kind="reflection",
        date="2026-05-14",
        section_markdown="Today's reflection.",
    )
    assert result["created"] is True
    assert drive.created[0]["name"] == "Evening Reflections — Week of 2026-05-11"
    assert drive.created[0]["parents"] == ["folder-reflections"]


def test_update_weekly_doc_finds_existing_doc(patched_clients) -> None:
    drive, docs = patched_clients
    drive._existing[("folder-briefs", "Morning Briefs — Week of 2026-05-11")] = "existing-doc-id"
    drive._export_text = "2026-05-13\nYesterday's brief.\n\n---\n\n"
    result = docs_tools.update_weekly_doc(
        kind="brief", date="2026-05-14", section_markdown="Today's brief."
    )
    assert result["file_id"] == "existing-doc-id"
    assert result["created"] is False
    assert result["updated"] is True
    # No new file created.
    assert drive.created == []
    # batchUpdate fired against the existing doc.
    assert len(docs.batch_updates) == 1
    doc_id, body = docs.batch_updates[0]
    assert doc_id == "existing-doc-id"


# ----------------------------- batchUpdate shape -----------------------------


def test_batch_update_prepends_with_heading_1(patched_clients) -> None:
    _, docs = patched_clients
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-14",
        section_markdown="Today's brief body.",
    )
    _, body = docs.batch_updates[0]
    requests = body["requests"]
    assert len(requests) == 2
    # First request: insertText at index 1 with date + body + separator.
    insert = requests[0]["insertText"]
    assert insert["location"]["index"] == 1
    assert insert["text"].startswith("2026-05-14\nToday's brief body.")
    assert insert["text"].endswith("---\n\n")
    # Second request: style the date line as HEADING_1.
    style = requests[1]["updateParagraphStyle"]
    assert style["range"]["startIndex"] == 1
    # "2026-05-14" is 10 chars + newline → endIndex = 1 + 10 + 1 = 12
    assert style["range"]["endIndex"] == 12
    assert style["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    assert style["fields"] == "namedStyleType"


# ----------------------------- idempotency ----------------------------------


def test_update_weekly_doc_skips_when_today_already_inserted(patched_clients) -> None:
    drive, docs = patched_clients
    drive._existing[("folder-briefs", "Morning Briefs — Week of 2026-05-11")] = "doc-with-today"
    # Existing plain-text export starts with today's date as the first line.
    drive._export_text = "2026-05-14\nAlready inserted earlier today.\n\n---\n\n"
    result = docs_tools.update_weekly_doc(
        kind="brief", date="2026-05-14", section_markdown="Re-run content."
    )
    assert result["updated"] is False
    assert result["duplicate"] is True
    assert result["created"] is False
    # batchUpdate must NOT fire on idempotent rerun.
    assert docs.batch_updates == []


def test_idempotency_check_ignores_blank_leading_lines(patched_clients) -> None:
    drive, docs = patched_clients
    drive._existing[("folder-briefs", "Morning Briefs — Week of 2026-05-11")] = (
        "doc-with-blank-prefix"
    )
    drive._export_text = "\n\n  \n2026-05-14\nbody\n"
    result = docs_tools.update_weekly_doc(kind="brief", date="2026-05-14", section_markdown="x")
    assert result["duplicate"] is True
    assert docs.batch_updates == []


# ----------------------------- input validation -----------------------------


def test_update_weekly_doc_rejects_invalid_kind(patched_clients) -> None:
    result = docs_tools.update_weekly_doc(kind="todo", date="2026-05-14", section_markdown="x")
    assert result["updated"] is False
    assert "kind must be" in result["error"]


def test_update_weekly_doc_rejects_empty_markdown(patched_clients) -> None:
    result = docs_tools.update_weekly_doc(kind="brief", date="2026-05-14", section_markdown="   ")
    assert result["updated"] is False
    assert "empty section_markdown" in result["error"]


def test_update_weekly_doc_rejects_invalid_date(patched_clients) -> None:
    result = docs_tools.update_weekly_doc(kind="brief", date="May 14", section_markdown="x")
    assert result["updated"] is False
    assert "ISO YYYY-MM-DD" in result["error"]


def test_update_weekly_doc_errors_without_folder_env(
    patched_clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BRAIN_BRIEFS_FOLDER_ID", raising=False)
    result = docs_tools.update_weekly_doc(kind="brief", date="2026-05-14", section_markdown="x")
    assert result["updated"] is False
    assert "BRAIN_BRIEFS_FOLDER_ID" in result["error"]


# ----------------------------- weekly review kind ---------------------------


def test_update_weekly_doc_review_uses_quarterly_title(patched_clients) -> None:
    """Reviews accumulate quarterly — title is 'Weekly Reviews — Q{N} {year}'."""
    drive, _ = patched_clients
    result = docs_tools.update_weekly_doc(
        kind="review",
        date="2026-05-15",
        section_markdown="Friday review prompt body.",
        section_label="Friday Prompt",
    )
    assert result["created"] is True
    assert drive.created[0]["name"] == "Weekly Reviews — Q2 2026"
    assert drive.created[0]["parents"] == ["folder-reviews"]
    # anchor_date is the first day of the quarter.
    assert result["anchor_date"] == "2026-04-01"


def test_update_weekly_doc_review_quarter_boundaries(patched_clients) -> None:
    """Sanity-check the Q1-Q4 mapping."""
    drive, _ = patched_clients
    for date in ("2026-01-05", "2026-04-01", "2026-07-15", "2026-12-31"):
        docs_tools.update_weekly_doc(
            kind="review",
            date=date,
            section_markdown="x",
            section_label="Friday Prompt",
        )
    titles = [c["name"] for c in drive.created]
    assert titles == [
        "Weekly Reviews — Q1 2026",
        "Weekly Reviews — Q2 2026",
        "Weekly Reviews — Q3 2026",
        "Weekly Reviews — Q4 2026",
    ]


def test_section_label_used_in_header_and_idempotency(patched_clients) -> None:
    """PROMPT and REFLECT can both write the same date because the
    section_label discriminates the H1."""
    drive, docs = patched_clients
    # Existing doc with the Friday Prompt section at the top.
    drive._existing[("folder-reviews", "Weekly Reviews — Q2 2026")] = "doc-quarterly"
    drive._export_text = "2026-05-15 — Friday Prompt\nprompt body\n---\n"

    # Friday's PROMPT re-run on the same date → duplicate detected.
    result_friday = docs_tools.update_weekly_doc(
        kind="review",
        date="2026-05-15",
        section_markdown="prompt body",
        section_label="Friday Prompt",
    )
    assert result_friday["duplicate"] is True
    # Sunday's REFLECT writes a DIFFERENT header → no collision.
    result_sunday = docs_tools.update_weekly_doc(
        kind="review",
        date="2026-05-17",
        section_markdown="sunday reflection body",
        section_label="Sunday Reflection",
    )
    assert result_sunday["duplicate"] is False
    assert result_sunday["updated"] is True
    # Verify the header line includes the label suffix.
    sunday_insert = docs.batch_updates[0][1]["requests"][0]["insertText"]
    assert sunday_insert["text"].startswith("2026-05-17 — Sunday Reflection\n")


def test_review_requires_reviews_folder_env(
    patched_clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BRAIN_REVIEWS_FOLDER_ID", raising=False)
    result = docs_tools.update_weekly_doc(
        kind="review",
        date="2026-05-15",
        section_markdown="x",
        section_label="Friday Prompt",
    )
    assert result["updated"] is False
    assert "BRAIN_REVIEWS_FOLDER_ID" in result["error"]


def test_review_rejected_when_not_in_kind_set(patched_clients) -> None:
    result = docs_tools.update_weekly_doc(kind="planning", date="2026-05-15", section_markdown="x")
    assert result["updated"] is False
    assert "kind must be 'brief', 'reflection', or 'review'" in result["error"]


# ----------------------------- wikilink linkification -----------------------


@pytest.fixture
def patched_wikilink_lookup(monkeypatch: pytest.MonkeyPatch):
    """Patch _lookup_galaxy_urls and return a mutable dict tests can fill.

    Keyed by LOWERCASE target name (matches the production signature).
    Tests insert (key → url) entries to simulate galaxy notes.
    """
    urls: dict[str, str] = {}
    monkeypatch.setattr(
        docs_tools,
        "_lookup_galaxy_urls",
        lambda targets: {t.lower(): urls[t.lower()] for t in targets if t.lower() in urls},
    )
    return urls


def test_resolved_wikilink_strips_brackets_and_adds_link(
    patched_clients, patched_wikilink_lookup
) -> None:
    _, docs = patched_clients
    patched_wikilink_lookup["client a"] = "https://drive.google.com/file/d/clienta-pi-id/view"
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-19",
        section_markdown="Reach out to [[Client A]] today.",
    )
    _, body = docs.batch_updates[0]
    requests = body["requests"]
    # Bracketed text was stripped from the inserted body.
    insert_text = requests[0]["insertText"]["text"]
    assert "[[Client A]]" not in insert_text
    assert "Reach out to Client A today." in insert_text
    # A single updateTextStyle for the link, AFTER the HEADING_1 request.
    link_requests = [r for r in requests if "updateTextStyle" in r]
    assert len(link_requests) == 1
    style = link_requests[0]["updateTextStyle"]
    assert style["textStyle"]["link"]["url"] == (
        "https://drive.google.com/file/d/clienta-pi-id/view"
    )
    assert style["fields"] == "link"
    # Range covers exactly "Client A" in the inserted text.
    body_start = insert_text.index("Client A")
    # Docs inserts at index 1 so add 1 to the relative offset.
    assert style["range"]["startIndex"] == 1 + body_start
    assert style["range"]["endIndex"] == 1 + body_start + len("Client A")


def test_unresolved_wikilink_preserves_brackets_no_link(
    patched_clients, patched_wikilink_lookup
) -> None:
    _, docs = patched_clients
    # No entries → nothing resolves.
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-19",
        section_markdown="Reach out to [[Nobody Special]] today.",
    )
    _, body = docs.batch_updates[0]
    requests = body["requests"]
    # Brackets stay verbatim; no updateTextStyle request.
    insert_text = requests[0]["insertText"]["text"]
    assert "[[Nobody Special]]" in insert_text
    assert not any("updateTextStyle" in r for r in requests)


def test_wikilink_alias_uses_alias_as_display(patched_clients, patched_wikilink_lookup) -> None:
    _, docs = patched_clients
    patched_wikilink_lookup["client a"] = "https://example.com/clienta"
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-19",
        section_markdown="Talk to [[Client A|the firm]] today.",
    )
    _, body = docs.batch_updates[0]
    requests = body["requests"]
    insert_text = requests[0]["insertText"]["text"]
    # Alias rendered as display text; bracketed form gone.
    assert "the firm" in insert_text
    assert "[[Client A" not in insert_text
    link_requests = [r for r in requests if "updateTextStyle" in r]
    assert len(link_requests) == 1
    style = link_requests[0]["updateTextStyle"]
    assert style["textStyle"]["link"]["url"] == "https://example.com/clienta"
    body_start = insert_text.index("the firm")
    assert style["range"]["startIndex"] == 1 + body_start
    assert style["range"]["endIndex"] == 1 + body_start + len("the firm")


def test_multiple_wikilinks_each_get_their_own_link(
    patched_clients, patched_wikilink_lookup
) -> None:
    _, docs = patched_clients
    patched_wikilink_lookup["client a"] = "https://example.com/clienta"
    patched_wikilink_lookup["client c studio"] = "https://example.com/clientc"
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-19",
        section_markdown="Both [[Client A]] and [[Client C Studio]] flagged.",
    )
    _, body = docs.batch_updates[0]
    link_requests = [r for r in body["requests"] if "updateTextStyle" in r]
    assert len(link_requests) == 2
    urls = sorted(r["updateTextStyle"]["textStyle"]["link"]["url"] for r in link_requests)
    assert urls == ["https://example.com/clienta", "https://example.com/clientc"]


def test_mixed_resolved_and_unresolved_wikilinks(patched_clients, patched_wikilink_lookup) -> None:
    _, docs = patched_clients
    patched_wikilink_lookup["client a"] = "https://example.com/clienta"
    docs_tools.update_weekly_doc(
        kind="brief",
        date="2026-05-19",
        section_markdown="[[Client A]] resolves, [[Mystery Co]] does not.",
    )
    _, body = docs.batch_updates[0]
    requests = body["requests"]
    insert_text = requests[0]["insertText"]["text"]
    # Resolved one has brackets stripped; unresolved keeps them.
    assert "Client A resolves" in insert_text
    assert "[[Mystery Co]] does not" in insert_text
    # Exactly one link request (for the resolved one).
    link_requests = [r for r in requests if "updateTextStyle" in r]
    assert len(link_requests) == 1


# --------------------------- section idempotency ----------------------------


def test_unlabeled_section_not_blocked_by_labeled_same_date() -> None:
    """An unlabeled date header (`2026-05-14`) is a prefix of a labeled one
    (`2026-05-14 — Friday Prompt`); the check must match the full line, so a
    labeled section first in the doc must NOT block an unlabeled insert."""
    plain = "2026-05-14 — Friday Prompt\nprompt body\n"
    # Unlabeled brief for the same date is NOT a duplicate.
    assert docs_tools._section_already_inserted(plain, "2026-05-14", None) is False
    # The labeled section itself IS detected as already present.
    assert docs_tools._section_already_inserted(plain, "2026-05-14", "Friday Prompt") is True


def test_unlabeled_section_detected_when_present() -> None:
    plain = "2026-05-14\nbrief body\n"
    assert docs_tools._section_already_inserted(plain, "2026-05-14", None) is True
