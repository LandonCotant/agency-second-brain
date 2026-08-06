"""Drive/Docs write tool for scheduled routines.

``update_weekly_doc`` prepends today's section to a single weekly
Google Doc (one per ``kind`` per week). Avoids the daily-doc explosion
that prompted the consolidation request 2026-05-14: instead of
~365 daily Briefs and ~365 daily Reflections per year, you get
~52/year/kind, each a 5-day chronological journal.

The tool is idempotent on (kind, date): re-running for the same day
detects the existing H1 date and returns without double-inserting.
That guards against scheduled-routine retries and manual reruns.

Auth: operator ADC impersonates ``asb-agent-triage-sa`` for Drive +
Docs calls (see ``clients._drive_scoped_creds``). Operator's ADC
itself can't get a Drive scope (ADC OAuth client allowlist drops it),
so we route through SA impersonation. Per ADR 0044 the rollup folders
must be shared with the SA's email as Editor.
"""

from __future__ import annotations

import re
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

from ...common.bq_helpers import parse_iso_date
from ...common.wikilink_parser import extract_wikilinks
from ..clients import docs_client, drive_client, get_config, query_rows

_GDOC_MIME = "application/vnd.google-apps.document"

# Mirrors ``common.wikilink_parser._WIKILINK_RE`` — captures target up to ``|``
# or ``]]`` and optionally consumes a ``|alias`` segment. We re-parse here
# instead of reusing because we need match positions, not just targets.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")


def _lookup_galaxy_urls(targets: list[str]) -> dict[str, str]:
    """Map lowercased wikilink target → ``source_drive_url`` for the most
    recent matching note in ``agent_outputs.notes``.

    Mirrors ``common.wikilink_parser.resolve_target`` (case-insensitive
    ``filename`` match, HIPAA-isolated rows excluded) but returns the
    Drive URL instead of the note_id, and batches all targets into one
    query. Missing targets are absent from the returned dict.
    """
    if not targets:
        return {}
    cfg = get_config()
    # Drive-sourced filenames keep their file extension in BQ
    # (``Client A.md``, ``call-brief-….md``, legacy ``Notes_…pdf``).
    # Wikilinks are written without extensions (``[[Client A]]``), so
    # strip the trailing ``.ext`` before comparing. Synthetic notes
    # (calendar_event, capture, etc.) have no extension and pass
    # through unchanged. ADR 0057 §1 explicitly promised this match.
    rows = query_rows(
        f"WITH ranked AS ( "  # noqa: S608
        f"  SELECT "
        f"    LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) AS key, "
        f"    source_drive_url AS url, "
        f"    ROW_NUMBER() OVER ( "
        f"      PARTITION BY LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) "
        f"      ORDER BY ingested_at DESC "
        f"    ) AS rn "
        f"  FROM `{cfg.project_id}.{cfg.notes_dataset}.{cfg.notes_table}` "
        f"  WHERE LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) "
        f"    IN UNNEST(@targets) "
        f"    AND COALESCE(hipaa_isolated, FALSE) = FALSE "
        f"    AND source_drive_url IS NOT NULL "
        f") "
        f"SELECT key, url FROM ranked WHERE rn = 1",
        parameters=[
            {
                "name": "targets",
                "type": "ARRAY_STRING",
                "value": [t.lower() for t in targets],
            }
        ],
    )
    return {r["key"]: r["url"] for r in rows}


def _linkify_wikilinks(section_md: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Resolve ``[[Name]]`` → strip brackets + return link ranges.

    For each wikilink that resolves to a note's ``source_drive_url``:
    the ``[[`` and ``]]`` are removed from the output text and the
    display range (alias if present, else the target name) is recorded
    so the caller can issue a Docs API ``updateTextStyle`` with a
    ``link.url`` field over that range. Unresolved wikilinks are kept
    verbatim (brackets intact) — the visible ``[[…]]`` is operator
    feedback that the target wasn't found.

    Returns ``(rewritten_md, [(start_offset, end_offset, url), …])``.
    Offsets are char positions within ``rewritten_md`` (the caller adds
    the doc-prefix offset before issuing the Docs API request).
    """
    targets = extract_wikilinks(section_md)
    urls = _lookup_galaxy_urls(targets) if targets else {}
    if not urls:
        return section_md, []

    out_parts: list[str] = []
    links: list[tuple[int, int, str]] = []
    cursor = 0
    out_len = 0
    for m in _WIKILINK_RE.finditer(section_md):
        target = m.group(1).strip()
        url = urls.get(target.lower())
        if url is None:
            chunk = section_md[cursor : m.end()]
            out_parts.append(chunk)
            out_len += len(chunk)
        else:
            prefix = section_md[cursor : m.start()]
            out_parts.append(prefix)
            out_len += len(prefix)
            # Display: alias if ``[[Target|alias]]``, else the target.
            inner = m.group(0)[2:-2]
            display = (inner.split("|", 1)[1] if "|" in inner else inner).strip()
            link_start = out_len
            out_parts.append(display)
            out_len += len(display)
            links.append((link_start, out_len, url))
        cursor = m.end()
    out_parts.append(section_md[cursor:])
    return "".join(out_parts), links


def _resolve_folder_id(kind: str) -> str | None:
    """Return the configured Drive folder ID for the kind, or None."""
    import os

    if kind == "brief":
        return os.environ.get("BRAIN_BRIEFS_FOLDER_ID") or None
    if kind == "reflection":
        return os.environ.get("BRAIN_REFLECTIONS_FOLDER_ID") or None
    if kind == "review":
        return os.environ.get("BRAIN_REVIEWS_FOLDER_ID") or None
    return None


def _quarter_of(d: date_cls) -> int:
    """Calendar quarter (1-4) for a date — Q1=Jan-Mar, Q2=Apr-Jun, etc."""
    return (d.month - 1) // 3 + 1


def _doc_title_for(kind: str, anchor_date: date_cls) -> str:
    """Build the title for the consolidated Doc this section lands in.

    Briefs + Reflections accumulate weekly (one Doc per week). Reviews
    accumulate quarterly (one Doc per quarter — 13 weekly reviews each).
    """
    if kind == "review":
        return f"Weekly Reviews — Q{_quarter_of(anchor_date)} {anchor_date.year}"
    week_monday = _monday_of_week(anchor_date)
    if kind == "brief":
        return f"Morning Briefs — Week of {week_monday.isoformat()}"
    return f"Evening Reflections — Week of {week_monday.isoformat()}"


def _monday_of_week(d: date_cls) -> date_cls:
    return d - timedelta(days=d.weekday())


def _find_doc(folder_id: str, title: str) -> str | None:
    """Return the fileId of the doc with this title in the folder, or None."""
    # Escape single quotes for the q= clause.
    safe_title = title.replace("'", "\\'")
    resp = (
        drive_client()
        .files()
        .list(
            q=(
                f"name = '{safe_title}' "
                f"and '{folder_id}' in parents "
                f"and mimeType = '{_GDOC_MIME}' "
                f"and trashed = false"
            ),
            fields="files(id, name)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_empty_doc(folder_id: str, title: str) -> str:
    """Create an empty Google Doc and return its fileId."""
    file = (
        drive_client()
        .files()
        .create(
            body={
                "name": title,
                "mimeType": _GDOC_MIME,
                "parents": [folder_id],
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return file["id"]


def _read_doc_plain_text(file_id: str) -> str:
    """Export an existing Doc as plain text. Used for idempotency check."""
    resp = drive_client().files().export(fileId=file_id, mimeType="text/plain").execute()
    # files.export returns bytes via the underlying http transport; the
    # googleapiclient wrapper auto-decodes for some mime types but not
    # all. Normalize to str.
    if isinstance(resp, bytes):
        return resp.decode("utf-8", errors="replace")
    return str(resp)


def _section_header(date_iso: str, section_label: str | None) -> str:
    """The H1 line for a prepended section.

    Without ``section_label`` (Briefs / Reflections): just the date.
    With ``section_label`` (Weekly Reviews — Friday Prompt / Sunday
    Reflection): ``{date} — {label}`` so PROMPT and REFLECT can each
    fire once on the same Doc without idempotency-colliding.
    """
    if section_label:
        return f"{date_iso} — {section_label}"
    return date_iso


def _section_already_inserted(
    existing_plain: str, date_iso: str, section_label: str | None
) -> bool:
    """Detect whether the section already exists at the top of the doc.

    Matches the EXACT header (``{date}`` or ``{date} — {label}``) as
    the first non-blank line. Must be an exact-line match, NOT a prefix:
    the header renders as a standalone line, and an unlabeled header
    (``{date}``) is a prefix of a labeled one (``{date} — {label}``), so
    ``startswith`` would let a labeled section block an unlabeled one if
    both kinds ever pointed at the same Doc.
    """
    if not existing_plain:
        return False
    header = _section_header(date_iso, section_label)
    for line in existing_plain.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped == header
    return False


def update_weekly_doc(
    kind: str,
    date: str,
    section_markdown: str,
    section_label: str | None = None,
) -> dict[str, Any]:
    """Prepend today's section to the consolidated Brief/Reflection/Review doc.

    USE THIS WHEN a scheduled routine has composed today's content
    and needs to deposit it in the right rollup Doc. Replaces the
    "create a new daily doc every day" pattern (~700 docs/year) with
    rollups: one Doc per week per kind for briefs + reflections
    (~104/year), one Doc per quarter for reviews (~4/year).

    DO NOT USE FOR:
      - Ad-hoc notes the user wants captured — use ``capture_note``.
      - Long-form documents the user is actively writing — use
        Anthropic's Drive MCP ``mcp__claude_ai_Google_Drive__create_file``
        for free-form Doc creation.
      - Any non-routine write path. This tool is intentionally narrow.

    Behavior:
      - Title is computed from ``kind`` + ``date``:
        - ``brief``: "Morning Briefs — Week of {monday}"
        - ``reflection``: "Evening Reflections — Week of {monday}"
        - ``review``: "Weekly Reviews — Q{N} {year}"
      - Searches the configured folder for that doc. Creates it if
        missing.
      - Prepends today's section at the top of the doc body, styling
        the first line (the section header) as ``HEADING_1``.
      - Idempotent: if a section with the same header (``{date}`` or
        ``{date} — {section_label}``) already appears as the first
        H1, returns ``{"updated": False, "duplicate": True}``.

    Args:
        kind: ``"brief"`` | ``"reflection"`` | ``"review"``.
        date: ISO date for today, e.g. ``"2026-05-14"``.
        section_markdown: The body content to insert below the H1.
        section_label: Optional discriminator appended to the H1 as
            ``{date} — {label}``. Used by Weekly Review routines so
            Friday PROMPT and Sunday REFLECT can both write to the
            quarterly Doc without idempotency-colliding
            (``"Friday Prompt"`` vs. ``"Sunday Reflection"``).

    Returns:
        ``{"file_id": str, "doc_url": str, "updated": bool,
        "created": bool, "duplicate": bool, "anchor_date": str}``.
        ``anchor_date`` is the week_monday (for brief/reflection) or
        the quarter-start ISO date (for review).
    """
    if kind not in ("brief", "reflection", "review"):
        return {
            "file_id": None,
            "updated": False,
            "error": f"kind must be 'brief', 'reflection', or 'review', got {kind!r}",
        }
    section_markdown = (section_markdown or "").strip()
    if not section_markdown:
        return {
            "file_id": None,
            "updated": False,
            "error": "empty section_markdown",
        }
    date_obj, date_err = parse_iso_date(date, field_name="date")
    if date_err is not None:
        return {"file_id": None, "updated": False, **date_err}

    folder_id = _resolve_folder_id(kind)
    if not folder_id:
        env_name = {
            "brief": "BRAIN_BRIEFS_FOLDER_ID",
            "reflection": "BRAIN_REFLECTIONS_FOLDER_ID",
            "review": "BRAIN_REVIEWS_FOLDER_ID",
        }[kind]
        return {
            "file_id": None,
            "updated": False,
            "error": f"{env_name} not set — configure in claude_desktop_config.json env",
        }

    title = _doc_title_for(kind, date_obj)
    # anchor_date is week-Monday for weekly rollups, first-of-quarter
    # for review rollups. Useful as a response field for callers that
    # want to know which rollup-bucket they wrote into.
    if kind == "review":
        anchor_date = date_cls(date_obj.year, 3 * (_quarter_of(date_obj) - 1) + 1, 1)
    else:
        anchor_date = _monday_of_week(date_obj)

    file_id = _find_doc(folder_id, title)
    created = False
    if not file_id:
        file_id = _create_empty_doc(folder_id, title)
        created = True

    # Idempotency: existing doc already contains a section with this
    # exact header (date + optional label).
    if not created:
        existing = _read_doc_plain_text(file_id)
        if _section_already_inserted(existing, date_obj.isoformat(), section_label):
            return {
                "file_id": file_id,
                "doc_url": f"https://docs.google.com/document/d/{file_id}/edit",
                "updated": False,
                "created": False,
                "duplicate": True,
                "anchor_date": anchor_date.isoformat(),
            }

    # Compose the prepend text: header line + body + separator.
    # The Docs API insertText inserts the literal text; paragraph
    # styling is applied in a follow-up updateParagraphStyle request.
    # Wikilinks that resolve to a Galaxy note's source_drive_url have
    # their ``[[…]]`` brackets stripped here and pick up a follow-up
    # updateTextStyle request with the resolved URL.
    header_line = _section_header(date_obj.isoformat(), section_label)
    body_md, link_ranges = _linkify_wikilinks(section_markdown)
    prepend_text = f"{header_line}\n{body_md}\n\n---\n\n"
    date_line_len = len(header_line)
    # First body char (just after the header's trailing newline) sits
    # at doc index 1 + len(header_line) + 1; body offsets map by adding
    # this prefix.
    body_doc_offset = 1 + date_line_len + 1

    # `index: 1` is the first valid insertion point (index 0 is the
    # doc's implicit start-of-body sentinel). For a freshly created
    # doc the body has a single empty paragraph at index 1.
    requests: list[dict[str, Any]] = [
        {
            "insertText": {
                "location": {"index": 1},
                "text": prepend_text,
            }
        },
        {
            "updateParagraphStyle": {
                "range": {
                    "startIndex": 1,
                    "endIndex": 1 + date_line_len + 1,  # +1 includes the trailing newline
                },
                "paragraphStyle": {"namedStyleType": "HEADING_1"},
                "fields": "namedStyleType",
            }
        },
    ]
    for link_start, link_end, url in link_ranges:
        requests.append(
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": body_doc_offset + link_start,
                        "endIndex": body_doc_offset + link_end,
                    },
                    "textStyle": {"link": {"url": url}},
                    "fields": "link",
                }
            }
        )
    docs_client().documents().batchUpdate(
        documentId=file_id,
        body={"requests": requests},
    ).execute()

    return {
        "file_id": file_id,
        "doc_url": f"https://docs.google.com/document/d/{file_id}/edit",
        "updated": True,
        "created": created,
        "duplicate": False,
        "anchor_date": anchor_date.isoformat(),
    }
