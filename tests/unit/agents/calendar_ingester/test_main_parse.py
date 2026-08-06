"""Unit tests for ``parse_calendar_configs`` in main.py.

The parser turns the ``CALENDAR_IDS`` env var into per-calendar
``(calendar_id, scope)`` pairs so each calendar can carry a distinct
``scope`` tag on the ``agent_outputs.notes`` rows it produces (ADR 0037
PKM merge dataset architecture — `agency` vs `personal` separation).
"""

from __future__ import annotations

from agency_brain.agents.calendar_ingester.main import parse_calendar_configs


def test_single_id_uses_default_scope():
    assert parse_calendar_configs("primary", "agency") == [("primary", "agency")]


def test_multiple_ids_no_scope_use_default():
    assert parse_calendar_configs("primary,foo@bar.com", "agency") == [
        ("primary", "agency"),
        ("foo@bar.com", "agency"),
    ]


def test_explicit_per_calendar_scope():
    assert parse_calendar_configs("primary,personal@example.com:personal", "agency") == [
        ("primary", "agency"),
        ("personal@example.com", "personal"),
    ]


def test_all_explicit_scopes():
    assert parse_calendar_configs("primary:agency,personal@example.com:personal", "agency") == [
        ("primary", "agency"),
        ("personal@example.com", "personal"),
    ]


def test_empty_scope_falls_back_to_default():
    # `id:` with empty scope segment falls back to the default.
    assert parse_calendar_configs("primary:", "agency") == [("primary", "agency")]


def test_whitespace_tolerant():
    assert parse_calendar_configs(" primary , personal@example.com : personal ", "agency") == [
        ("primary", "agency"),
        ("personal@example.com", "personal"),
    ]


def test_empty_items_skipped():
    assert parse_calendar_configs(",primary,,", "agency") == [("primary", "agency")]


def test_empty_csv_returns_empty_list():
    assert parse_calendar_configs("", "agency") == []


def test_rpartition_handles_colon_in_id():
    # If a calendar ID somehow contains a colon (rare — Google calendar
    # IDs don't normally, but be defensive), rpartition keeps the rightmost
    # segment as the scope so the colon-containing id stays intact.
    assert parse_calendar_configs("weird:id:with:colons:personal", "agency") == [
        ("weird:id:with:colons", "personal")
    ]
