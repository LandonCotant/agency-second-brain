from __future__ import annotations

import pytest
from agency_brain.common.memory_bank import (
    InMemoryMemoryBank,
    MemoryBankNotConfigured,
    VertexMemoryBank,
    build_namespace,
)


def test_build_namespace_basic() -> None:
    assert build_namespace("risk-watcher", "client-123") == "risk-watcher/client-123"


def test_build_namespace_with_subkey() -> None:
    assert (
        build_namespace("triage", "user@agency.com", "cached-classifications")
        == "triage/user@agency.com/cached-classifications"
    )


@pytest.mark.parametrize(
    "agent_id,entity_id",
    [
        ("", "client-123"),
        ("Risk-Watcher", "client-123"),  # uppercase rejected
        ("risk-watcher", ""),
        ("risk watcher", "client-123"),  # space rejected
        ("risk-watcher", "client/123"),  # slash inside segment rejected
    ],
)
def test_build_namespace_rejects_invalid(agent_id: str, entity_id: str) -> None:
    with pytest.raises(ValueError):
        build_namespace(agent_id, entity_id)


def test_in_memory_round_trip() -> None:
    mb = InMemoryMemoryBank()
    ns = build_namespace("risk-watcher", "client-123")

    assert mb.read(ns, "baseline") is None

    mb.write(ns, "baseline", {"roas": 3.2, "weeks": 8})
    assert mb.read(ns, "baseline") == {"roas": 3.2, "weeks": 8}

    mb.delete(ns, "baseline")
    assert mb.read(ns, "baseline") is None


def test_in_memory_returns_isolated_copies() -> None:
    """Mutating returned dicts must not bleed back into the store."""
    mb = InMemoryMemoryBank()
    ns = build_namespace("triage", "global")
    mb.write(ns, "k", {"v": 1})

    out = mb.read(ns, "k")
    assert out is not None
    out["v"] = 999

    assert mb.read(ns, "k") == {"v": 1}


def test_vertex_memory_bank_requires_id() -> None:
    with pytest.raises(MemoryBankNotConfigured):
        VertexMemoryBank(memory_bank_id=None)
    with pytest.raises(MemoryBankNotConfigured):
        VertexMemoryBank(memory_bank_id="")


def test_vertex_memory_bank_methods_unimplemented_in_pr1() -> None:
    mb = VertexMemoryBank(memory_bank_id="projects/x/locations/us-central1/memoryBanks/y")
    with pytest.raises(NotImplementedError):
        mb.read("triage/global", "k")
    with pytest.raises(NotImplementedError):
        mb.write("triage/global", "k", {"v": 1})
    with pytest.raises(NotImplementedError):
        mb.delete("triage/global", "k")
