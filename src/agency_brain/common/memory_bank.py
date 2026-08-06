"""Agent Memory Bank client + namespace convention.

PRD §3 picks Agent Memory Bank as the persistence layer for per-client
baselines (Risk Watcher §6.4) and per-user goal context (Goal Steward §4).
The convention here is documented in `docs/memory_bank_namespaces.md` —
every WS-G agent reads/writes through the same namespace shape so the
dataset is legible and HIPAA isolation is auditable.

The Vertex-backed implementation is wired in PR #2 once the Agent Engine
+ Memory Bank instance is provisioned. PR #1 ships the Protocol, the
in-memory implementation used by tests/dev, and the namespace builder.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Protocol

# Allowed characters per namespace segment. Hyphenated lowercase agent IDs
# (e.g. `risk-watcher`) and stable scope/key slugs. Email addresses are
# allowed as scope segments for user-keyed namespaces.
_SEGMENT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-.@]*$")


class MemoryBankNotConfigured(RuntimeError):
    """`VertexMemoryBank` instantiated without a memory_bank_id.

    Until PR #2 deploys the Agent Engine, no Vertex Memory Bank instance
    exists. Production agents should fail fast rather than silently no-op.
    """


class MemoryBank(Protocol):
    def read(self, namespace: str, key: str) -> dict[str, Any] | None: ...
    def write(self, namespace: str, key: str, value: dict[str, Any]) -> None: ...
    def delete(self, namespace: str, key: str) -> None: ...


def build_namespace(agent_id: str, entity_id: str, subkey: str | None = None) -> str:
    """Build a Memory Bank namespace per the convention.

    Format: `{agent_id}/{entity_id}[/{subkey}]`. See
    `docs/memory_bank_namespaces.md` for the registry of allowed patterns.
    """
    segments = [agent_id, entity_id]
    if subkey is not None:
        segments.append(subkey)
    for seg in segments:
        if not seg or not _SEGMENT_PATTERN.match(seg):
            raise ValueError(
                f"invalid memory bank namespace segment: {seg!r}. "
                "Segments must match [a-z0-9][a-z0-9_\\-.@]*"
            )
    return "/".join(segments)


class InMemoryMemoryBank:
    """Dict-backed implementation. Test-only and dev-only.

    Thread-safe so parallel test workers don't tear up the shared state.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def read(self, namespace: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._store.get((namespace, key))
            return None if value is None else dict(value)

    def write(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._store[(namespace, key)] = dict(value)

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._store.pop((namespace, key), None)


class VertexMemoryBank:
    """Wraps the Vertex AI Agent Engine Memory Bank API.

    The actual SDK calls land in PR #2 alongside the Agent Engine instance.
    PR #1 ships only the constructor surface so WS-G workstreams can wire
    against it without waiting; calling `read`/`write` raises until then.
    """

    def __init__(self, memory_bank_id: str | None) -> None:
        if not memory_bank_id:
            raise MemoryBankNotConfigured(
                "VertexMemoryBank requires memory_bank_id. Set TB_MEMORY_BANK_ID "
                "after PR #2 provisions the Agent Engine + Memory Bank instance."
            )
        self._memory_bank_id = memory_bank_id

    def read(self, namespace: str, key: str) -> dict[str, Any] | None:
        raise NotImplementedError(
            "VertexMemoryBank.read lands in PR #2 with the Agent Engine instance."
        )

    def write(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        raise NotImplementedError(
            "VertexMemoryBank.write lands in PR #2 with the Agent Engine instance."
        )

    def delete(self, namespace: str, key: str) -> None:
        raise NotImplementedError(
            "VertexMemoryBank.delete lands in PR #2 with the Agent Engine instance."
        )
