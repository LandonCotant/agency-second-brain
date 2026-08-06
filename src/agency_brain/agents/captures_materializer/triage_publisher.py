"""Thin Pub/Sub publisher wrapper for the Captures materializer.

Most of the publish surface is in ``dispatch.py`` (envelope shape +
ordering keys are Kind-specific). This module just owns the topic-path
construction so ``main.py`` can swap in a fake publisher in tests.
"""

from __future__ import annotations


def topic_path(*, project_id: str, topic_name: str = "asb-triage-input") -> str:
    """Canonical Pub/Sub topic path for ``asb-triage-input``."""
    return f"projects/{project_id}/topics/{topic_name}"
