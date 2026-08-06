"""REFLECT-mode replacement for the v1 Gmail-draft surface (ADR 0044).

Wraps ``common.drive_doc_writer.DriveDocClient`` with the Reflection's
title convention and parent-folder env binding. The agent's REFLECT
branch calls ``write(...)`` once per recipient per day; the Doc lands in
``Brain/Areas/Reflections/`` and Drive's auto-conversion turns the HTML
body into a Google Doc.

Returned ``ReflectionDocResult`` carries both the doc id and webViewLink
so the agent can attach the URL to the Chat-card notification + persist
both into ``agent_outputs.evening_reflections.reflection_doc_{id,url}``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ...common.drive_doc_writer import (
    DocCreationResult,
    DriveDocClient,
    DriveDocWriteError,
)

log = logging.getLogger("agency_brain.agents.evening_reflection.reflection_doc_writer")


@dataclass(frozen=True)
class ReflectionDocResult:
    doc_id: str
    doc_url: str


class ReflectionDocWriter:
    """Creates the daily Reflection Doc in ``Brain/Areas/Reflections/``.

    Mirrors ``GmailDraftsClient`` ergonomically — ``write(run_date,
    body_html)`` returns enough to populate the BQ row + Chat card.
    Failures are logged + raised so the agent emits a single audit row
    with ``success=False`` and the caller's writer.write reflects it.
    """

    def __init__(
        self,
        *,
        drive_client: DriveDocClient,
        parent_folder_id: str,
    ) -> None:
        if not parent_folder_id:
            raise ValueError("parent_folder_id is required — set BRAIN_AREAS_REFLECTIONS_FOLDER_ID")
        self._drive = drive_client
        self._parent = parent_folder_id

    def write(
        self,
        *,
        run_date: date,
        body_html: str,
        title_suffix: str = "Reflection",
    ) -> ReflectionDocResult:
        """Create the daily Reflection Doc.

        Title format ``YYYY-MM-DD-DDD <suffix>`` (e.g.
        ``2026-05-07-Thu Reflection``) so Drive sorts chronologically
        and humans can scan the folder by date.
        """
        title = self.title_for(run_date, title_suffix)
        try:
            result: DocCreationResult = self._drive.create_doc(
                parent_folder_id=self._parent,
                title=title,
                body_html=body_html,
            )
        except DriveDocWriteError:
            log.exception("reflection_doc_writer.create_failed parent=%s", self._parent)
            raise
        return ReflectionDocResult(doc_id=result.doc_id, doc_url=result.web_view_link)

    @staticmethod
    def title_for(run_date: date, suffix: str = "Reflection") -> str:
        return f"{run_date.strftime('%Y-%m-%d-%a')} {suffix}"
