"""``resolve_reference`` — placeholder until its increment lands (HANDOFF §10)."""

from __future__ import annotations

from typing import Any

from app.errors import internal
from app.mcp.protocol import Tool, ToolContext


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raise internal("resolve_reference is not implemented in this build.")


TOOL = Tool(
    name="resolve_reference",
    description="Parse a wiki URL and say whether it can be read from here.",
    input_schema={"type": "object", "properties": {}},
    output_schema=None,
    scope=None,
    handler=handle,
)
