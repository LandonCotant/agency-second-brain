"""Tests for shared BQ SQL fragments + small validators."""

from __future__ import annotations

from datetime import date

import pytest
from agency_brain.common.bq_helpers import (
    exclude_hipaa,
    filter_recency,
    parse_iso_date,
)

# --------------------------------------------------------------- exclude_hipaa


def test_exclude_hipaa_with_alias_default_kind() -> None:
    assert exclude_hipaa("a") == "COALESCE(a.hipaa_excluded, FALSE) = FALSE"


def test_exclude_hipaa_without_alias() -> None:
    assert exclude_hipaa() == "COALESCE(hipaa_excluded, FALSE) = FALSE"


def test_exclude_hipaa_isolated_kind() -> None:
    assert exclude_hipaa("n", kind="isolated") == "COALESCE(n.hipaa_isolated, FALSE) = FALSE"


def test_exclude_hipaa_isolated_kind_no_alias() -> None:
    assert exclude_hipaa(kind="isolated") == "COALESCE(hipaa_isolated, FALSE) = FALSE"


def test_exclude_hipaa_rejects_unknown_kind() -> None:
    """Unknown kind raises rather than silently emitting bad SQL."""
    with pytest.raises(ValueError, match="unknown kind"):
        exclude_hipaa("a", kind="raw")


# --------------------------------------------------------------- filter_recency


def test_filter_recency_with_alias() -> None:
    assert (
        filter_recency("n.created_at", 30)
        == "n.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
    )


def test_filter_recency_without_alias() -> None:
    assert (
        filter_recency("flagged_at", 90)
        == "flagged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)"
    )


# --------------------------------------------------------------- parse_iso_date


def test_parse_iso_date_valid() -> None:
    d, err = parse_iso_date("2026-05-19")
    assert d == date(2026, 5, 19)
    assert err is None


def test_parse_iso_date_invalid_format_returns_error_dict() -> None:
    d, err = parse_iso_date("05/19/2026")
    assert d is None
    assert err is not None
    assert "error" in err
    assert "ISO YYYY-MM-DD" in err["error"]
    assert "'05/19/2026'" in err["error"]


def test_parse_iso_date_non_string_returns_error_dict() -> None:
    d, err = parse_iso_date(20260519)  # type: ignore[arg-type]
    assert d is None
    assert err is not None
    assert "error" in err


def test_parse_iso_date_uses_field_name_in_error() -> None:
    _, err = parse_iso_date("bad", field_name="start_date")
    assert err is not None
    assert "start_date" in err["error"]
