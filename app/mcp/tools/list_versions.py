"""``list_versions`` — placeholder until its increment lands (HANDOFF §10)."""

from __future__ import annotations

from typing import Any

from app.errors import internal
from app.mcp.protocol import Tool, ToolContext


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raise internal("list_versions is not implemented in this build.")


TOOL = Tool(
    name="list_versions",
    description="The version chain of one path, newest first.",
    input_schema={"type": "object", "properties": {}},
    output_schema=None,
    scope="wiki.read",
    handler=handle,
)
