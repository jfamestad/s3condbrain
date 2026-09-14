"""``move_article`` — placeholder until its increment lands (HANDOFF §10)."""

from __future__ import annotations

from typing import Any

from app.errors import internal
from app.mcp.protocol import Tool, ToolContext


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raise internal("move_article is not implemented in this build.")


TOOL = Tool(
    name="move_article",
    description="Relocate an article, leaving a permanent forward pointer at the old path.",
    input_schema={"type": "object", "properties": {}},
    output_schema=None,
    scope="wiki.write",
    handler=handle,
)
