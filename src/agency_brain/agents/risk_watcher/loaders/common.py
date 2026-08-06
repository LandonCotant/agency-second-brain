"""Shared loader primitives.

`BQQueryClient` is the dependency shape every loader (and the writer's
parameterized dedup pre-check) accepts. The production adapter lives
in `agents/risk_watcher/main.py`; tests substitute a `_FakeBQ` keyed
by SQL substring.
"""

from __future__ import annotations

from typing import Protocol


class BQQueryClient(Protocol):
    """Subset of ``google.cloud.bigquery.Client`` we depend on."""

    def query_rows(self, sql: str) -> list[dict]: ...


def in_list(values: list[str]) -> str:
    """Render a Python list as a BQ IN-list literal.

    Values are Airtable record ids (`rec` + 14 base62 chars) per ADR
    0022 — no user-supplied free-text flows here, so direct quoting is
    safe. We still defensively escape single quotes.
    """
    quoted = [f"'{v.replace(chr(39), chr(39) + chr(39))}'" for v in values]
    return ",".join(quoted)
