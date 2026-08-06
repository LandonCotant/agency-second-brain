"""Shared BQ SQL fragments + small validators used across MCP tools and agents.

The fragments here exist because the same SQL pattern (HIPAA exclusion,
recency window, ISO date parse) was being re-typed in 8+ places in
``mcp_server/tools/*``. Centralizing gives one place to fix bugs and
ensures every consumer behaves identically — particularly important
for HIPAA exclusion where a missed filter is a compliance breach.

Audit that surfaced this module (2026-05-19): ``person.py`` filtered
accounts on the raw Airtable checkbox column ``hipaa`` while
``read.py`` filtered the same table on the cascade-derived
``hipaa_excluded``. Both columns exist on ``airtable_replica.accounts``;
``hipaa_excluded`` is the right one (matches how every other replica
table is filtered, derived at sync time per ``sync/hipaa_filters.py``).
The ``kind=`` parameter on ``exclude_hipaa`` makes the choice explicit
so this kind of drift can't recur.

These helpers are intentionally simple — strings + small validators,
no SQL DSL or query builder. If a fragment doesn't fit your callsite
verbatim, write the SQL inline; don't bend the helper.
"""

from __future__ import annotations

from datetime import date, datetime

# HIPAA flag column per table family. Both are BOOL with NULL allowed
# (hence the COALESCE wrap in the emitted fragment).
_HIPAA_COLUMNS: dict[str, str] = {
    # airtable_replica.* (cascades from accounts.HIPAA at sync time).
    # Tables: accounts, contacts, projects, tasks, captures, contracts,
    # goals, goal_scores, risk_profiles, service_catalog, team.
    "excluded": "hipaa_excluded",
    # agent_outputs.notes (set when source Drive folder is HIPAA-tagged).
    "isolated": "hipaa_isolated",
}


def exclude_hipaa(alias: str = "", *, kind: str = "excluded") -> str:
    """SQL fragment excluding HIPAA-flagged rows.

    Returns a ``COALESCE({alias}.{col}, FALSE) = FALSE`` clause where
    ``col`` is chosen by ``kind``:

      - ``"excluded"`` (default): ``hipaa_excluded`` — for any
        ``airtable_replica.*`` table.
      - ``"isolated"``: ``hipaa_isolated`` — for ``agent_outputs.notes``.

    ``alias`` is the SQL table alias (e.g. ``"a"``, ``"n"``). Pass empty
    string when no alias is needed.

    Examples::

        exclude_hipaa("a")                  # "COALESCE(a.hipaa_excluded, FALSE) = FALSE"
        exclude_hipaa("n", kind="isolated") # "COALESCE(n.hipaa_isolated, FALSE) = FALSE"
        exclude_hipaa()                     # "COALESCE(hipaa_excluded, FALSE) = FALSE"

    The raw Airtable checkbox column ``hipaa`` (on ``accounts``) is
    intentionally NOT supported here — ``hipaa_excluded`` is the
    cascade-derived column every other code path uses.
    """
    try:
        column = _HIPAA_COLUMNS[kind]
    except KeyError:
        raise ValueError(
            f"exclude_hipaa: unknown kind {kind!r}; expected one of " f"{sorted(_HIPAA_COLUMNS)}"
        ) from None
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}{column}, FALSE) = FALSE"


def filter_recency(column: str, days: int) -> str:
    """SQL fragment filtering rows to the last ``days`` days.

    ``column`` is fully-qualified (include alias if needed), e.g.
    ``"n.created_at"`` or ``"r.flagged_at"``. Uses ``>=`` (inclusive at
    the boundary). Pre-existing callsites that used ``>`` are converted
    to ``>=``; the difference at the day boundary is negligible and
    consistency beats the edge-case behavior.
    """
    return f"{column} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)"


def parse_iso_date(value: str, *, field_name: str = "date") -> tuple[date | None, dict | None]:
    """Parse ISO ``YYYY-MM-DD`` or return an MCP-shaped error dict.

    Returns ``(date, None)`` on success, ``(None, {"error": ...})`` on
    failure. MCP tools convention: validation failures return a dict
    with an ``error`` key rather than raising — caller threads the
    error dict back to the LLM verbatim, which the LLM can then
    surface to the user.
    """
    try:
        return datetime.fromisoformat(value).date(), None
    except (ValueError, TypeError):
        return None, {"error": f"{field_name} must be ISO YYYY-MM-DD, got {value!r}"}
