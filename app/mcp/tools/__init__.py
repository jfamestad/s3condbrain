"""Tool registry. Each tool module exposes a module-level ``TOOL: Tool``.

Skeleton scope (HANDOFF §11.4): create_article, read_article, list_folder,
update_article. Later increments append to ``_MODULES`` only.
"""

from __future__ import annotations

import importlib

from app.mcp.protocol import Tool

_MODULES = (
    "create_article",
    "read_article",
    "list_folder",
    "update_article",
)


def registry() -> dict[str, Tool]:
    """Import every tool module and return ``{name: Tool}`` in declaration order."""
    tools: dict[str, Tool] = {}
    for mod in _MODULES:
        tool: Tool = importlib.import_module(f"app.mcp.tools.{mod}").TOOL
        tools[tool.name] = tool
    return tools


__all__ = ["registry"]
