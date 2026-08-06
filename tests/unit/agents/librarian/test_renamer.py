"""Tests for ``librarian.renamer`` (Phase G+ filename canonicalization)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agency_brain.agents.librarian.renamer import (
    canonical_filename,
    derive_description_slug,
    derive_topic_slug,
    is_anonymous_filename,
    maybe_rename,
)

# --------------------------------------------------------------- anonymous detection


@pytest.mark.parametrize(
    "name",
    [
        "Untitled.md",
        "Untitled (3).md",
        "Untitled document.gdoc",
        "Document.pdf",
        "Document (5).pdf",
        "Notes_260502_160251.pdf",
        "voice-memo-3.m4a",
        "voice_memo.m4a",
        "VoiceMemo.m4a",
        "Recording-7.m4a",
        "IMG_0123.jpg",
        "IMG-9999.heic",
        "Screenshot.png",
        "Screenshot 2026-05-07 at 1.53.42 PM.png",
        "20260507_142233.jpg",
        "2026-05-07_142233.png",
    ],
)
def test_is_anonymous_filename_recognizes_auto_generated(name: str) -> None:
    assert is_anonymous_filename(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "call-brief-2026-05-07-clienta-site-review.md",
        "clienta-pi-strategy-2026-q3.pdf",
        "ClientC Brief Feedback.gdoc",
        "Acme Co Onboarding Notes.pdf",
        "tax-q3-summary-final.pdf",
    ],
)
def test_is_anonymous_filename_preserves_deliberate_names(name: str) -> None:
    assert is_anonymous_filename(name) is False


# --------------------------------------------------------------- topic slug


def test_derive_topic_slug_skips_generic_subfolders() -> None:
    assert derive_topic_slug("clients/06_CLIENT_A/08_MEETING NOTES") == "client-a"
    assert derive_topic_slug("clients/04_CLIENT_B_STORM/01_STRATEGY") == "client-b-storm"
    assert derive_topic_slug("clients/05_CLIENT_C_STUDIO/06_DELIVERABLES") == "client-c-studio"


def test_derive_topic_slug_uses_leaf_when_not_generic() -> None:
    assert derive_topic_slug("playbooks/local-service") == "local-service"
    assert derive_topic_slug("brain/personal/wellness") == "wellness"


def test_derive_topic_slug_handles_path_with_only_one_segment() -> None:
    assert derive_topic_slug("clients") == "clients"


def test_derive_topic_slug_falls_back_to_leaf_when_all_generic() -> None:
    # _uncategorized is in the generic set; everything is generic; use leaf.
    assert derive_topic_slug("_uncategorized") == "uncategorized"


def test_derive_topic_slug_strips_number_prefix() -> None:
    assert derive_topic_slug("clients/06_CLIENT_A") == "client-a"
    assert derive_topic_slug("clients/00-acme") == "acme"


def test_derive_topic_slug_empty_path_returns_uncategorized() -> None:
    assert derive_topic_slug("") == "uncategorized"


# --------------------------------------------------------------- description slug


def test_derive_description_slug_uses_suggested() -> None:
    assert derive_description_slug("Site Copy Review") == "site-copy-review"
    assert derive_description_slug("Q3 Budget Draft") == "q3-budget-draft"


def test_derive_description_slug_falls_back_when_empty() -> None:
    assert derive_description_slug(None) == "note"
    assert derive_description_slug("") == "note"
    assert derive_description_slug(None, fallback="memo") == "memo"


def test_derive_description_slug_truncates_long_input() -> None:
    long = "this is a very very very very very long description that should be truncated"
    out = derive_description_slug(long)
    assert len(out) <= 50


# --------------------------------------------------------------- canonical filename


def test_canonical_filename_format() -> None:
    name = canonical_filename(
        date=datetime(2026, 5, 7, tzinfo=UTC),
        topic="clienta-pi",
        description="site-copy-review",
        extension="md",
    )
    assert name == "2026-05-07_clienta-pi_site-copy-review.md"


def test_canonical_filename_normalizes_extension() -> None:
    name = canonical_filename(
        date=datetime(2026, 5, 7, tzinfo=UTC),
        topic="t",
        description="d",
        extension=".PDF",
    )
    assert name.endswith(".pdf")


def test_canonical_filename_handles_no_extension() -> None:
    name = canonical_filename(
        date=datetime(2026, 5, 7, tzinfo=UTC),
        topic="t",
        description="d",
        extension="",
    )
    assert name == "2026-05-07_t_d"


# --------------------------------------------------------------- maybe_rename


def test_maybe_rename_renames_anonymous_files() -> None:
    new = maybe_rename(
        original_name="Notes_260502_160251.pdf",
        dest_folder_path="clients/06_CLIENT_A/08_MEETING NOTES",
        file_modified_time=datetime(2026, 5, 7, tzinfo=UTC),
        suggested_description="site copy review",
    )
    assert new == "2026-05-07_client-a_site-copy-review.pdf"


def test_maybe_rename_returns_none_for_deliberate_names() -> None:
    new = maybe_rename(
        original_name="call-brief-2026-05-07-clienta-site-review.md",
        dest_folder_path="clients/06_CLIENT_A/08_MEETING NOTES",
        file_modified_time=datetime(2026, 5, 7, tzinfo=UTC),
        suggested_description="site copy review",
    )
    assert new is None


def test_maybe_rename_uses_fallback_description() -> None:
    new = maybe_rename(
        original_name="Untitled.md",
        dest_folder_path="brain/personal",
        file_modified_time=datetime(2026, 5, 7, tzinfo=UTC),
        suggested_description=None,
        fallback_description="memo",
    )
    assert new == "2026-05-07_personal_memo.md"
