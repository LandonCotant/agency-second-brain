"""Tests for ``LibrarianAreasIndex``."""

from __future__ import annotations

from agency_brain.agents.librarian.areas_index import (
    LibrarianAreasIndex,
    find_folder_by_path,
    parse_excluded_names_env,
    parse_roots_env,
)
from agency_brain.agents.librarian.models import AreaFolder


class _FakeDriveListClient:
    def __init__(self, *, tree: dict[str, list[dict]] | None = None) -> None:
        self._tree = tree or {}

    def list_files(self, *, folder_id: str, page_size: int, fields: str) -> list[dict]:
        return list(self._tree.get(folder_id, []))


def _folder(file_id: str, name: str) -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }


def _file(file_id: str, name: str) -> dict:
    return {"id": file_id, "name": name, "mimeType": "application/pdf"}


def test_list_candidates_walks_2_levels() -> None:
    drive = _FakeDriveListClient(
        tree={
            "areas-root": [
                _folder("clients", "clients"),
                _folder("playbooks", "playbooks"),
                _file("readme", "README.gdoc"),  # ignored — not a folder
            ],
            "clients": [
                _folder("clienta", "clienta-pi"),
                _folder("jro", "client-c-studio"),
            ],
            "playbooks": [_folder("ls", "local-service")],
            "clienta": [],
            "jro": [],
            "ls": [],
        }
    )
    idx = LibrarianAreasIndex(drive_client=drive, areas_root_id="areas-root", max_depth=4)
    candidates = idx.list_candidates()
    paths = sorted(f.path for f in candidates)
    assert paths == [
        "areas/clients",
        "areas/clients/client-c-studio",
        "areas/clients/clienta-pi",
        "areas/playbooks",
        "areas/playbooks/local-service",
    ]


def test_excludes_underscore_uncategorized() -> None:
    drive = _FakeDriveListClient(
        tree={
            "areas-root": [
                _folder("u", "_uncategorized"),
                _folder("c", "clients"),
            ],
            "u": [],
            "c": [],
        }
    )
    idx = LibrarianAreasIndex(drive_client=drive, areas_root_id="areas-root")
    paths = [f.path for f in idx.list_candidates()]
    assert "areas/clients" in paths
    assert all("_uncategorized" not in p for p in paths)


def test_drive_failure_returns_empty() -> None:
    class _BoomDrive:
        def list_files(self, **_):
            raise RuntimeError("drive down")

    idx = LibrarianAreasIndex(drive_client=_BoomDrive(), areas_root_id="areas-root")
    assert idx.list_candidates() == []


def test_find_folder_by_path_case_insensitive() -> None:
    candidates = [
        AreaFolder(id="i1", name="clienta-pi", path="clients/clienta-pi"),
        AreaFolder(id="i2", name="jro", path="clients/client-c-studio"),
    ]
    found = find_folder_by_path(candidates, "Clients/Clienta-Pi")
    assert found is not None and found.id == "i1"


def test_find_folder_by_path_returns_none_when_missing() -> None:
    candidates = [AreaFolder(id="i1", name="x", path="clients/x")]
    assert find_folder_by_path(candidates, "clients/missing") is None
    assert find_folder_by_path(candidates, None) is None


# --------------------------------------------------------------- Phase G


def test_multi_root_walks_each_root_with_label_prefix() -> None:
    drive = _FakeDriveListClient(
        tree={
            "brain-root": [_folder("personal", "personal")],
            "personal": [_folder("p-wellness", "wellness")],
            "p-wellness": [],
            "clients-root": [_folder("clienta", "clienta-pi")],
            "clienta": [_folder("strategy", "01_STRATEGY")],
            "strategy": [],
        }
    )
    idx = LibrarianAreasIndex(
        drive_client=drive,
        roots=[("brain", "brain-root"), ("clients", "clients-root")],
    )
    candidates = idx.list_candidates()
    paths = sorted(f.path for f in candidates)
    assert paths == [
        "brain/personal",
        "brain/personal/wellness",
        "clients/clienta-pi",
        "clients/clienta-pi/01_STRATEGY",
    ]
    # root_label / root_id propagate through every walked candidate
    by_path = {f.path: f for f in candidates}
    assert by_path["clients/clienta-pi/01_STRATEGY"].root_id == "clients-root"
    assert by_path["clients/clienta-pi/01_STRATEGY"].root_label == "clients"
    assert by_path["brain/personal"].root_id == "brain-root"


def test_user_supplied_exclusions_skip_folders() -> None:
    drive = _FakeDriveListClient(
        tree={
            "root": [
                _folder("clients", "05_CLIENTS"),
                _folder("finance", "02_FINANCE & ACCOUNTING"),
            ],
            "05_CLIENTS": [],
            "02_FINANCE & ACCOUNTING": [],
        }
    )
    idx = LibrarianAreasIndex(
        drive_client=drive,
        roots=[("solutions", "root")],
        excluded_names=frozenset({"02_FINANCE & ACCOUNTING"}),
    )
    paths = [f.path for f in idx.list_candidates()]
    assert "solutions/05_CLIENTS" in paths
    assert all("FINANCE" not in p for p in paths)


def test_excluded_names_case_insensitive() -> None:
    drive = _FakeDriveListClient(
        tree={
            "root": [_folder("a", "FOO_BAR"), _folder("b", "OK")],
            "FOO_BAR": [],
            "OK": [],
        }
    )
    idx = LibrarianAreasIndex(
        drive_client=drive,
        roots=[("r", "root")],
        excluded_names=frozenset({"foo_bar"}),
    )
    paths = [f.path for f in idx.list_candidates()]
    assert paths == ["r/OK"]


def test_legacy_areas_root_id_works_for_back_compat() -> None:
    drive = _FakeDriveListClient(tree={"areas-root": [_folder("c", "clients")], "c": []})
    idx = LibrarianAreasIndex(drive_client=drive, areas_root_id="areas-root")
    paths = [f.path for f in idx.list_candidates()]
    assert paths == ["areas/clients"]


def test_constructor_rejects_no_roots() -> None:
    import pytest

    drive = _FakeDriveListClient()
    with pytest.raises(ValueError):
        LibrarianAreasIndex(drive_client=drive)
    with pytest.raises(ValueError):
        LibrarianAreasIndex(drive_client=drive, roots=[("brain", "")])


def test_parse_roots_env_happy_path() -> None:
    out = parse_roots_env("brain:abc, clients:def , solutions:xyz")
    # ADR 0054 — 2-segment entries default to bucket="areas".
    assert out == [
        ("areas", "brain", "abc"),
        ("areas", "clients", "def"),
        ("areas", "solutions", "xyz"),
    ]


def test_parse_roots_env_skips_malformed() -> None:
    out = parse_roots_env("good:id, no_colon, : , label:")
    assert out == [("areas", "good", "id")]


def test_parse_roots_env_three_segment_bucket_label_id() -> None:
    """ADR 0054 — ``bucket=label:folder_id`` carries the bucket through."""
    out = parse_roots_env("brain:abc, resources=resources:res-id, clients:cli-id")
    assert out == [
        ("areas", "brain", "abc"),
        ("resources", "resources", "res-id"),
        ("areas", "clients", "cli-id"),
    ]


def test_parse_roots_env_bucket_is_lowercased() -> None:
    out = parse_roots_env("RESOURCES=resources:abc")
    assert out == [("resources", "resources", "abc")]


def test_parse_excluded_names_env_strips_whitespace() -> None:
    out = parse_excluded_names_env("  foo , bar  ,baz")
    assert out == frozenset({"foo", "bar", "baz"})
    assert parse_excluded_names_env("") == frozenset()


# --------------------------------------------------------------- ADR 0054


def test_constructor_accepts_three_tuple_roots_propagates_bucket() -> None:
    """ADR 0054 — 3-tuple ``(bucket, label, folder_id)`` flows through to
    each emitted ``AreaFolder.bucket``."""
    drive = _FakeDriveListClient(
        tree={
            "brain-root": [_folder("p", "personal")],
            "p": [],
            "res-root": [_folder("t", "templates")],
            "t": [],
        }
    )
    idx = LibrarianAreasIndex(
        drive_client=drive,
        roots=[
            ("areas", "brain", "brain-root"),
            ("resources", "resources", "res-root"),
        ],
    )
    candidates = idx.list_candidates()
    by_path = {f.path: f for f in candidates}
    assert by_path["brain/personal"].bucket == "areas"
    assert by_path["resources/templates"].bucket == "resources"


def test_constructor_two_tuple_roots_defaults_bucket_to_areas() -> None:
    """Back-compat: existing 2-tuple ``roots=[("label", "id"), ...]``
    callers default to ``bucket='areas'`` without code changes."""
    drive = _FakeDriveListClient(tree={"r": [_folder("c", "clients")], "c": []})
    idx = LibrarianAreasIndex(drive_client=drive, roots=[("clients", "r")])
    candidates = idx.list_candidates()
    assert all(f.bucket == "areas" for f in candidates)


def test_legacy_areas_root_id_defaults_bucket_to_areas() -> None:
    drive = _FakeDriveListClient(tree={"areas-root": [_folder("c", "clients")], "c": []})
    idx = LibrarianAreasIndex(drive_client=drive, areas_root_id="areas-root")
    candidates = idx.list_candidates()
    assert all(f.bucket == "areas" for f in candidates)
