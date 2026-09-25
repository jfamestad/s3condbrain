"""Tool registry. Each tool module exposes a module-level ``TOOL: Tool``.

All twelve tools of HANDOFF §10, in the table's order. A module that is not yet
implemented ships a placeholder whose handler returns a 500 envelope.
"""

from __future__ import annotations

import importlib

from app.mcp.protocol import Tool

_MODULES = (
    "search",
    "list_folder",
    "read_article",
    "resolve_reference",
    "shared_with_me",
    "list_versions",
    "read_version",
    "create_article",
    "update_article",
    "move_article",
    "archive_article",
    "unarchive_article",
)


def registry() -> dict[str, Tool]:
    """Import every tool module and return ``{name: Tool}`` in declaration order."""
    tools: dict[str, Tool] = {}
    for mod in _MODULES:
        tool: Tool = importlib.import_module(f"app.mcp.tools.{mod}").TOOL
        tools[tool.name] = tool
    return tools


__all__ = ["registry"]
