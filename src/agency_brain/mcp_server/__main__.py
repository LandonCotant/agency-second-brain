"""Entry point so ``asb-mcp-server`` / ``python -m agency_brain.mcp_server``
both work. ADR 0051 §2 (local stdio)."""

from __future__ import annotations


def main() -> None:
    from .server import mcp

    mcp.run()  # default transport is stdio


if __name__ == "__main__":
    main()
