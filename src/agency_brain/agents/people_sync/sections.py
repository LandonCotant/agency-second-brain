"""Section-aware body manipulation for People Sync (ADR 0057 §5).

A People Sync body has alternating user-prose H2 sections and
``<!-- AUTO -->``-tagged H2 sections that the bq_enricher regenerates
each tick. Example:

    # Client A

    ## Who they are
    User-written prose. NEVER OVERWRITTEN.

    ## Active engagements
    <!-- AUTO: populated by asb-people-sync -->
    - Q3 campaign

    ## Recent activity
    <!-- AUTO: populated by asb-people-sync -->
    - 2026-05-18: email exchange

    ## Connections
    User-written wikilinks. NEVER OVERWRITTEN.

``replace_auto_section(body, "## Active engagements", new_content)`` finds
the matching H2 and rewrites the body of that section ONLY if the
section contains the AUTO marker as its first non-blank line. User-
prose sections (no AUTO marker) are left untouched even if the caller
passes their header by mistake — defensive against bugs that could
clobber user content.
"""

from __future__ import annotations

import re

AUTO_MARKER = "<!-- AUTO: populated by asb-people-sync -->"


_H2_LINE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def replace_auto_section(body: str, h2_header: str, new_section_content: str) -> str:
    """Replace the content of one H2 section in ``body``.

    ``h2_header`` is the full header line e.g. ``"## Active engagements"``.
    Matching is exact (whitespace-normalized) on the header line text.

    ``new_section_content`` is the markdown that goes BELOW the header
    (without the H2 line itself). It should START with the
    ``AUTO_MARKER`` comment so the section stays self-documenting; the
    function does not auto-prepend the marker.

    Safety rules:
      - If the section's first non-blank line is NOT the AUTO marker,
        the function returns the body unchanged (user-prose protection).
      - If the header doesn't exist, the function returns the body
        unchanged.
      - The closing boundary is the next ``## `` header OR EOF.
    """
    sections = _split_into_sections(body)
    out_parts: list[str] = []
    replaced = False
    for header_line, content in sections:
        if not replaced and header_line.strip() == h2_header.strip() and _is_auto_section(content):
            content = _ensure_trailing_newline(new_section_content)
            replaced = True
        out_parts.append(header_line + content)
    result = "".join(out_parts)
    return result


def _split_into_sections(body: str) -> list[tuple[str, str]]:
    """Split body into [(header_line_with_newline, content), ...].

    The first tuple may have an empty header (preamble before the first
    H2 — typically the H1 ``# Name`` and any intro paragraphs). All
    subsequent tuples start with an ``## `` line including its trailing
    newline.
    """
    lines = body.splitlines(keepends=True)
    sections: list[tuple[str, str]] = []
    current_header: str = ""
    current_content: list[str] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((current_header, "".join(current_content)))
            current_header = line
            current_content = []
        else:
            current_content.append(line)
    sections.append((current_header, "".join(current_content)))
    return sections


def _is_auto_section(content: str) -> bool:
    """True iff the first non-blank line of the section content is the AUTO marker."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped == AUTO_MARKER
    return False


def _ensure_trailing_newline(s: str) -> str:
    if not s.endswith("\n"):
        return s + "\n"
    return s
