"""``search`` — placeholder until its increment lands (HANDOFF §10)."""

from __future__ import annotations

from typing import Any

from app.errors import internal
from app.mcp.protocol import Tool, ToolContext


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raise internal("search is not implemented in this build.")


TOOL = Tool(
    name="search",
    description="Find articles by words in their title, description or tags.",
    input_schema={"type": "object", "properties": {}},
    output_schema=None,
    scope="wiki.read",
    handler=handle,
)
